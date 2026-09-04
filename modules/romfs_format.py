#!/usr/bin/env python3
"""The romfs format: what every packer and parser has to agree on.

path_hash and hash_table_size compute what switch-tools computes, because a
bucket is only findable if everything reading or writing an image agrees:

  https://github.com/switchbrew/switch-tools/blob/master/src/romfs.c
"""
import collections
import struct

# No entry: an end-of-chain link, and an empty hash bucket.
EMPTY = 0xFFFFFFFF

# A chain link, a hash bucket and a name length are all 32-bit -- one
# constant rather than three, because it is one fact.
LINK_STRUCT = "<I"
LINK_SIZE = struct.calcsize(LINK_STRUCT)

# Names are padded to four, so every entry and every table lands on four. A
# link off this boundary is not pointing at an entry.
TABLE_ALIGN = 4


def align_up(n):
    """n rounded up to the boundary entries and tables sit on."""
    return (n + TABLE_ALIGN - 1) & ~(TABLE_ALIGN - 1)


# How each is laid out:
#
#   header      its own size, then (offset, size) for each of the four tables
#   directory   parent, sibling, first child, first file, hash link, name length
#   file        parent, sibling, data offset, data size, hash link, name length
#
# Sizes are calculated from the layouts rather than written beside them:
# a size written out is the layout stated twice, and two statements of one
# fact can drift.
HEADER_STRUCT = "<10q"
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)

DIR_ENTRY_STRUCT = "<6I"
DIR_ENTRY_SIZE = struct.calcsize(DIR_ENTRY_STRUCT)

FILE_ENTRY_STRUCT = "<IIqqII"
FILE_ENTRY_SIZE = struct.calcsize(FILE_ENTRY_STRUCT)

# The header's ten fields, named. Read positionally this is header[7] for the
# file table and header[9] for the data, which is how a wrong index looks
# exactly like a right one -- both are plausible offsets into the same image.
Header = collections.namedtuple("Header", (
    "size "
    "dir_hash_offset dir_hash_size "
    "dir_table_offset dir_table_size "
    "file_hash_offset file_hash_size "
    "file_table_offset file_table_size "
    "file_data_offset"))

# The root directory's entry, which is where a walk starts. First in the
# directory table because the format has nowhere else to say where it is:
# nothing points at the root, so a reader has to already know.
ROOT_ENTRY_OFFSET = 0

# The link fields inside an entry. Both kinds open with (parent, sibling); a
# directory continues with (first child, first file); both end with (hash link,
# name length), which puts the hash link two links from the end. Derived from
# the layouts rather than written out, so changing one moves the other.
#
# The format calls that field the hash; what it holds is the link that makes a
# bucket a chain.
PARENT_LINK_OFFSET = 0
SIBLING_LINK_OFFSET = LINK_SIZE
DIR_CHILD_LINK_OFFSET = 2 * LINK_SIZE
DIR_FIRST_FILE_LINK_OFFSET = 3 * LINK_SIZE
DIR_HASH_LINK_OFFSET = DIR_ENTRY_SIZE - 2 * LINK_SIZE
FILE_HASH_LINK_OFFSET = FILE_ENTRY_SIZE - 2 * LINK_SIZE

# Where the file data starts. A real romfs is data-first: the partition sits at
# 0x200 and the tables follow it, which is what ams assumes when it reads a
# source file's offset -- nothing ever consults a source header for this.
FILE_PARTITION_OFFSET = 0x200

# What every file's data is placed on, which is what puts every file on an
# 8-byte word. Whether a build honours it is asked of an image it produced
# rather than asserted here -- this file is the format taken as given.
FILE_DATA_ALIGN = 0x10


def path_hash(parent, name_bytes):
    """(parent entry offset, name) -> hash. Rotate right five, xor the byte.

    The same hash switch-tools' calc_path_hash produces, which any reader of
    the format has to. Names arrive as bytes because that is what the format
    hashes: UTF-8, one byte at a time.
    """
    h = (parent ^ 123456789) & 0xFFFFFFFF
    for b in name_bytes:
        h = ((h >> 5) | (h << 27)) & 0xFFFFFFFF
        h ^= b
    return h


def hash_table_size(num_entries):
    """Buckets for this many entries.

    The same count switch-tools' romfs_get_hash_table_count arrives at. The
    intent was the smallest prime at least as large as the entry count; the
    sieve stops at 17, so a reader has to expect the number that produces
    rather than the one it was aiming for.
    """
    if num_entries < 3:
        return 3
    if num_entries < 19:
        return num_entries | 1
    count = num_entries
    while (count % 2 == 0 or count % 3 == 0 or count % 5 == 0 or count % 7 == 0
           or count % 11 == 0 or count % 13 == 0 or count % 17 == 0):
        count += 1
    return count


# A stretch of an image, and what to call it. Ends are exclusive.
Region = collections.namedtuple("Region", "name start end")


def regions(header, prefix=""):
    """The regions of a romfs with that header, in file order.

    Where a romfs keeps its parts is the format's to say, which is why it is
    said here. The data partition is not a table but the whole of what the
    files sit in, so it is named for what it holds -- and it is the one region
    the header gives no size, so it is taken to end where the first table
    begins.

    A region of nothing is left out: a romfs with no files has no partition,
    and a row of zero says nothing.
    """
    named = ((prefix + "header", 0, HEADER_SIZE),
             (prefix + "file data", header.file_data_offset,
              header.dir_hash_offset),
             (prefix + "dir hash", header.dir_hash_offset,
              header.dir_hash_offset + header.dir_hash_size),
             (prefix + "dir table", header.dir_table_offset,
              header.dir_table_offset + header.dir_table_size),
             (prefix + "file hash", header.file_hash_offset,
              header.file_hash_offset + header.file_hash_size),
             (prefix + "file table", header.file_table_offset,
              header.file_table_offset + header.file_table_size))
    return [Region(*r) for r in named if r[2] > r[1]]


def header_at(image_bytes):
    """The header at the front of those bytes."""
    return Header(*struct.unpack(HEADER_STRUCT, image_bytes[:HEADER_SIZE]))
