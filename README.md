# Ozonde

A tool to measure what `fs.mitm`, inside an `ams.mitm` build, costs to serve a modded game its romfs:\
peak memory, `fs*` calls, bytes read and written, and write amplification.

Point it at any `ams_mitm.elf` to get a report.\
(Building `ams.mitm` from Atmosphère leaves one in `release/` or `debug/`\
under `stratosphere/ams_mitm/out/nintendo_nx_arm64_armv8a/`.)

The name is a contraction of *ozonesonde*: an instrument launched through the stratosphere to profile ozone.\
Released into the thing it measures, taking readings on the way up, reporting back.\
(`ams.mitm` is part of **Stratosphère**, a component from **Atmosphère**.)


# How does it work?

The ELF is mapped under **Unicorn** and **called into, not booted**.\
Everything reached runs as the ELF's own code, apart from the calls that would leave the process.

Eight functions are called by name:

1. `ams::ncm::Initialize`
2. `ams::fs::SetEnabledAutoAbort`
3. `ams::init::InitializeAllocator`
4. `ams::mitm::fs::OpenGlobalSdCardFileSystem`
5. **`ams::mitm::fs::romfs::ConfigureDynamicHeap`**
6. **`ams::mitm::fs::GetLayeredRomfsStorage`**
7. **`ams::mitm::fs::LayeredRomfsStorageImpl::InitializeImpl`**
8. `ams::mitm::fs::LayeredRomfsStorageImpl::Read`

1st to 4th stand in for the startup a boot would have done.\
**5th to 7th serve the game its romfs, and every figure in the report comes from them**.\
8th is called afterwards just to check the romfs.


# Layout

Who imports whom.

`run` turns the command line into premises and hands them to `bench`, which launches the
build and holds what it cost; the result is then handed to `report`, which renders it.

```
run                        → the command line: what to measure, and what it came to
├─ bench                   → what is known about ams.mitm, and the launch it measures
│  ├─ progress             → signs of life on a terminal
│  ├─ guest                → the ELF, mapped under Unicorn: symbols, calls, faults
│  │  └─ blackbox          → the flight recorder: what the build says, and how it dies
│  ├─ boundaries           → which of its calls would leave the process, from the binary
│  ├─ fake_services        → fs, ncm and settings answered, every other service refused
│  ├─ fake_meta            → what the system reports installed: the game, and any update
│  ├─ fake_sd              → the SD it reads, and its mirror on disk
│  ├─ hos_memory           → the memory it is given, and which pool each byte came from
│  ├─ fake_romfs           → the game it is serving, and the mod laid over it
│  │  └─ romfs_model       → a romfs shaped like TOTK's, at any size
│  ├─ romfs_pack ───┐      → writes a romfs as an image
│  ├─ romfs_format ─┤      → the entry layout, the hash and the bucket count
│  ├─ romfs_check ──┘      → reads the finished romfs back the way a game would
│  └─ accounting           → what the launch cost, charged to what it was spent on
└─ report                  → every figure, and the words and widths around it
```


# Requirements

- Python 3.8+
- [unicorn](https://pypi.org/project/unicorn/) 2.x
- [pyelftools](https://pypi.org/project/pyelftools/)
- `nm`, from the *binutils* package, or `aarch64-none-elf-nm`, from *devkitA64*


# Usage

```sh
<python> run.py <ams_mitm.elf> <x> [--patched]
       [--loose o=N,a=N] [--bin o=N,a=N] [--dyn-heap a=N,s=N] [--fresh-sd]
```

`<python>` depends on your Python install (e.g. **CPython**, **PyPy** or **uv**).

`--help`, or no arguments at all, prints the usage, arguments and options in the terminal.

A run prints its header, then asks before touching anything: enter to go ahead, `n` to stop.\
Piped or redirected, it never asks.


## Arguments

**`<ams_mitm.elf>`**: the build to measure.

**`<x>`**: how many files the base romfs holds.\
  Range: 1 to 500000.


## Options

**`--patched`**: the game has a fixed update installed.\
  The generated romfs changes slightly with it.

**`--loose o=N,a=N`**: a mod, as loose files under `romfs/`.\
  `o` is how many files it overrides, `a` how many it adds.\
  Range: 0 to 500000 for both. Default `o=1,a=0`, or `o=0,a=0` if `--bin` is named.

**`--bin o=N,a=N`**: a mod, packed into `romfs.bin`.\
  `o` is how many files it overrides, `a` how many it adds.\
  Range: 0 to 500000 for both. Default `o=0,a=0`.

Given both, the two take different files: what one overrides the other does not, and what one\
adds the other does not, so their counts add up. Between them they cannot override more than `x`.

One overridden file is the least that still makes `fs.mitm` build.

**`--dyn-heap a=N,s=N`**: the dynamic heap a build may take.\
  `a` is how many MiB it takes from the application pool, `s` how many from the system pool.\
  Range: 0 to 32 for both. Default `a=22,s=0`.

**`--fresh-sd`**: empty `sdmc/` first, for a genuinely cold run.\
  Without it `sdmc/` persists between runs, as a real SD does.\
  How ams is set up survives either way.


# Notes

- The program id is invented and fixed: `0100BADC0FFEE000`.
- **Mods never cross the mirror.** `romfs/` and `romfs.bin` under `atmosphere/contents/*/` are blocked both ways,\
  the mods handed to the build are always the generated ones, never whatever sits on `sdmc/`.
- **The static buffer (currently 12 MiB) belongs to the whole `ams.mitm` process, not to `fs.mitm`.** A run drives\
  only `fs.mitm`, so the row is its use alone, and what the other six modules take never appears in a report.
- **The abort `DynamicHeap::Map` takes is not reproduced.** Every map the build asks the OS for is\
  granted here, so the refusal a console can answer with, the one that abort follows, never happens.
- **The generated romfs is deterministic, and shaped like TOTK's.** Paths and sizes are pure functions\
  of the file index, so the same `x` gives byte-identical input.
- **An N GiB romfs does not cost N GiB of host RAM** (500k files cost less than 500 MiB).\
  Nothing but the header and the directory/file tables is ever built,\
  and the packer is minimal: just enough of the format to be a valid input.
- **Both digests are taken over the entry list, never over the whole image.**\
  Two runs agreeing on `Digest (sha-256: paths, sizes, and offsets)` packed the romfs identically.\
  Agreeing on `Digest (sha-256: paths and sizes)` alone means the same files, packed differently.


# Credits

- [**Atmosphère**](https://github.com/Atmosphere-NX/Atmosphere): obvious.
- The [**switchbrew wiki**](https://switchbrew.org/wiki/Main_Page): the docs and specs behind everything faked here.
- [**switch-tools**](https://github.com/switchbrew/switch-tools/blob/master/src/romfs.c): the romfs format.
- The [**ReSwitched**](https://discord.gg/ZdqEhed) Discord: valuable insights.


# ⚠ Warning ⚠

Most of this was written using **Claude**.
