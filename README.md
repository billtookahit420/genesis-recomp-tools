# GenesisRecomp Tools

Converts Sega Genesis / Mega Drive ROMs from your own collection into
native PC recompilations, using the **segagenesisrecomp** framework
by **Matthew Stanley (mstan) / RetroPortingToolkit**. Part of the
Recomp Tools family (PSX / NES / GB / SNES / GBA / N64 / Genesis).

## How Genesis recompilation works here

A Genesis recomp is **not** produced from a ROM alone. The framework's
shared runner executes a *per-game integration profile*: the game's
RAM layout plus discovery data derived from a disassembly of that
exact game (seed functions, subroutine and jump-table lists,
annotations), and a small spec file describing the game's lifecycle
hooks. A Sonic profile does not describe another 68000 game — the
framework's own guide says so.

This tool is therefore **profile-based**, like the N64 tool: it
converts exactly the games a profile exists for and flags everything
else as *needs review* with the honest reason. Profiles can be added
as upstream integrations land.

**Bundled profile:** Sonic the Hedgehog — **JUE REV00 only**
(CRC32 `F9394E97`, 512 KiB, serial `GM 00001009-00`). REV01 and other
revisions are different code and are not qualified targets yet; the
probe identifies them and says so.

## Usage

1. Run `genesis-recomp-tools.sh` (Linux) or `genesis-recomp-tools.bat`
   (Windows). First run creates a Python venv and installs PySide6 +
   Pillow; the framework template is cloned and the host recompiler
   built automatically on the first conversion.
2. Add ROMs (`.bin` / `.md` / `.gen` / `.smd`). The probe parses the
   Genesis header, verifies its checksum, and matches the image
   against the profiles. Interleaved `.smd` copier images are
   deinterleaved before hashing, so an `.smd` dump of a qualified
   game matches exactly.
3. Convert. Each game produces `<Game>-Linux.zip` / `<Game>-Windows.zip`
   in the output folder: the executable, its staged `assets/`
   launcher files, a README, optional RetroAchievements files, and
   box art under `artwork/`.

A built game contains **no ROM**: at launch you pick your own dump in
the launcher, which CRC-verifies it before starting.

## RetroAchievements

Wired in the family style: an rcheevos module compiled into the game
(`RC_CONSOLE_MEGA_DRIVE = 1`), hashing the ROM the launcher picked
with rcheevos' own Mega Drive algorithm and reading the runner's
work-RAM shadow for achievement logic. Login and unlocks pop up as
toasts through the launcher's runtime UI (the engine moves the
template's recomp-ui pin to the upstream revision that provides the
toast API and verifies it is present before relying on it). Put
`retroachievements.toml` next to the executable, or point Settings at
your saved copy to stamp it into every package.

Note: RetroAchievements counts unlocks from recomp clients as Casual
(softcore) — the service only honours hardcore from approved clients,
and this tool never spoofs another client's identity.

## Netplay

The framework ships rollback netplay for two-player titles upstream,
but the bundled profile's game (Sonic the Hedgehog) is single-player
— its spec declares one player and there is nothing to connect. The
tool offers no netplay toggle for it rather than a dead button. The
wiring path exists if a two-player profile is added.

## BIOS

None. Genesis cartridges need no BIOS; the runtime asks for none and
nothing is shipped.

## Verification status

- Framework + `GenesisRecomp` recompiler: **built from source** and
  exercised (it generated the full 32-part C output for a test image).
- Probe: **validated** against synthetic header fixtures, including
  an interleaved `.smd` round-trip that hashes identically to the
  plain image (SMD block order verified against the format's
  documentation: even bytes first, then odd).
- Whole pipeline: **run end-to-end in this environment** with a
  synthetic header fixture standing in for the ROM — scaffold →
  recompile → full build → package → the packaged executable boots
  to the real launcher (box art on screen), and pointing it at the
  fixture produces the designed refusal: "ROM identity mismatch …
  CRC32=B1E7C4F0, expected=F9394E97. Refusing to execute
  address-specific recompiled code."
- RetroAchievements module: **compile-verified** inside the game
  target (spec hook, runner UI bind, rcheevos). In-game hash /
  login / toast proof is pending a real ROM (and this sandbox's
  route to the RA service returns "No response" in any case).
- A real-game conversion + the ROM census: **pending a ROM set from
  the user's own collection** — this tool only converts the user's
  own dumps, and none had arrived when this build was made. The
  first conversion run with a qualified Sonic the Hedgehog
  (JUE REV00) dump is the remaining proof.
- Windows packages: MinGW cross-build, **compile-verified here**
  (full build + package produced in this environment): the
  host-built recompiler generates the game's code (a Windows-built
  generator could not run during a Linux-hosted build), the app's
  own SDL2 MinGW kit is provisioned automatically, and an entry
  shim forwards SDL2main's `SDL_main` to the runner's plain `main`
  (MSVC's SDL2main accommodates it; MinGW's does not). Runtime on
  real Windows is untested.
- Two upstream source fixes are applied to staged copies: a hex
  literal ending in `E` directly followed by `+`
  (`0xFE5E+oscillators`) is valid for Clang/MSVC tokenizers but
  rejected by GCC — the engine inserts a space in the staged game
  sources so the stock GCC toolchain builds them; and runner
  headers that include `<SDL2/SDL.h>` get the SDL2 include parent
  added for MinGW builds, whose CMake package only exposes the
  inner include dir.

## Credits & licenses

- **segagenesisrecomp / GenesisRecomp / SonicTheHedgehogRecomp** —
  Matthew Stanley (mstan), RetroPortingToolkit. PolyForm
  Noncommercial 1.0.0.
- **recomp-ui** — same team, MIT.
- Framework third parties (see the framework's
  THIRD-PARTY-LICENSES): ymfm (BSD-3-Clause), m68k-recomp-core,
  z80-recomp-core, recomp-net, rbengine, SDL2 (zlib license),
  minicoro (Unlicense/MIT-0), clowncommon (ISC).
- **rcheevos** (RetroAchievements) — MIT.
- Box art: libretro-thumbnails (game art belongs to its respective
  owners).

Unofficial fan tool; not affiliated with SEGA. Sonic the Hedgehog is
a trademark of SEGA.
