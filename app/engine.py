#!/usr/bin/env python3
"""GenesisRecomp Tools - engine.

Converts Sega Genesis / Mega Drive ROMs from the user's own
collection into native PC recompilations with the segagenesisrecomp
framework (Matthew Stanley / RetroPortingToolkit), packaged the same
way as the rest of the Recomp Tools family.

How this framework differs from the cartridge siblings (GB/SNES/GBA):
a Genesis recomp is NOT produced from a ROM alone. The shared runner
executes a per-game integration: a game.toml carrying the game's RAM
layout plus disassembly-derived discovery data (seed functions,
subroutine and jump-table lists, annotations), and one translation
unit defining the game's GameSpec. "A Sonic configuration does not
automatically describe another 68000 game" (the framework's own
GETTING_STARTED.md). So this tool is PROFILE-based, like the N64
tool: it converts exactly the games an integration profile exists
for, and flags everything else needs-review with the honest reason.

Profiles ship in the app (PROFILES below); the framework marks which
ROM revisions are qualified targets, and the probe matches a ROM to
a profile by full-image CRC32 + size. The bundled profile is the
framework's flagship: Sonic the Hedgehog (JUE REV00).

Layout of a conversion (upstream's productised game project,
SonicTheHedgehogRecomp, is the template):
  template project (framework + recomp-ui pinned as submodules)
    -> stage a copy, drop the ROM at game/sonic.bin
    -> the build itself runs GenesisRecomp on the ROM + game.toml
       and generates the game's C into the build tree
    -> CMake/Ninja builds the runner + generated code + launcher
    -> package: exe + staged assets/ + README + (optional) RA files

CLI:
  engine.py toolbox
  engine.py probe --rom PATH
  engine.py run --job FILE.json

Job JSON:
  {"roms": [paths], "output_dir": path,
   "ra": {"enabled": bool, "popups": bool, "toml": path-or-empty},
   "targets": {"linux": bool, "windows": bool}, "jobs": int,
   "force": bool}
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
BUNDLE_RA = APP_DIR / "bundle" / "ra"
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"
TOOLS_DIR = APP_DIR / "tools"

SONIC_REPO_URL = "https://github.com/mstan/SonicTheHedgehogRecomp.git"
FRAMEWORK_REPO_URL = "https://github.com/mstan/segagenesisrecomp.git"
TEMPLATE_DIRNAME = "SonicTheHedgehogRecomp"
# The game project pins recomp-ui a few hours before the runtime-UI
# toast API landed upstream (the RetroAchievements pop-up channel).
# The engine moves the template's recomp-ui submodule to this pin —
# recomp-ui's current HEAD, same-day successor of the shipped pin,
# launcher API unchanged — and verifies the toast symbol is present
# before relying on it. If the move cannot be made, RA still builds;
# pop-ups degrade to log lines and say so.
RECOMP_UI_TOAST_PIN = "817c2985d371f8d19d70d9d1d9629425ed6cfd68"

ROM_EXTS = {".bin", ".md", ".gen", ".smd"}
# The runner's ROM window (genesis_runtime.h: g_rom is 4 MiB).
ROM_WINDOW_MAX = 0x400000


class GenesisError(Exception):
    pass


def log_line(log, msg: str) -> None:
    print(msg, flush=True)
    if log:
        log(msg)


def run(cmd, *, cwd=None, env=None, log=None, timeout=None) -> subprocess.CompletedProcess:
    log_line(log, "+ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log_line(log, line)
    if proc.returncode != 0:
        raise GenesisError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}")
    return proc


def camel(title: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", title) if w)


# ---------------------------------------------------------------------------
# Integration profiles
#
# A profile is a qualified per-game integration: the exact ROM image
# (CRC32 + size), where the ROM lands in the template project, which
# spec translation unit carries its frame hooks, and the CMake target
# that produces the game. Only add a profile when the framework (or a
# productised game project on it) actually ships the integration.
# ---------------------------------------------------------------------------

PROFILES = {
    "sonic1": {
        "id": "sonic1",
        "title": "Sonic the Hedgehog",
        "camel": "SonicTheHedgehog",
        "crc32": 0xF9394E97,
        "size": 0x80000,
        "serial_hint": "GM 00001009",
        "revision": "JUE REV00",
        "rom_dest": "game/sonic.bin",
        "spec_rel": "game/sonic1_spec.c",
        "target": "SonicTheHedgehogRecomp",
        "lib_targets": ["sonic1"],
        "exe": "SonicTheHedgehogRecomp",
        "players": 1,
        "netplay": False,
    },
}


def profile_for_probe(probe: dict):
    for prof in PROFILES.values():
        if probe.get("crc32") == prof["crc32"] and probe.get("size") == prof["size"]:
            return prof
    return None


# ---------------------------------------------------------------------------
# Probe: Genesis cartridge header + image identity
#
# Header layout (on the plain, deinterleaved image):
#   0x100 system string ("SEGA GENESIS" / "SEGA MEGA DRIVE" / ...)
#   0x110 copyright, 0x120 domestic title, 0x150 overseas title
#   0x180 serial ("GM 00001009-00"), 0x18E header checksum (BE u16)
#   0x190 I/O support, 0x1A0/0x1A4 ROM start/end, 0x1A8/0x1AC RAM range
#   0x1B0 SRAM info, 0x1F0 region codes
# The header checksum is the sum of the big-endian u16 words from
# 0x200 to the end of the image.
#
# .smd images carry a 512-byte copier header and 16 KiB blocks whose
# bytes are interleaved (even bytes first, then odd); hashing or
# parsing them raw would identify garbage, so the probe deinterleaves
# first and reports the image's true identity.
# ---------------------------------------------------------------------------

def _u16be(b: bytes, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def _u32be(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _clean_title(raw: bytes) -> str:
    s = raw.decode("ascii", errors="replace")
    s = s.replace("\x00", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def is_smd_image(data: bytes) -> bool:
    """Super Magic Drive interleaved image: file size is 512 more
    than a multiple of 16 KiB and the copier header carries the
    0xAA 0xBB magic at bytes 8-9."""
    if len(data) < 512 + 16384:
        return False
    if (len(data) - 512) % 16384 != 0:
        return False
    return data[8] == 0xAA and data[9] == 0xBB


def deinterleave_smd(data: bytes) -> bytes:
    body = data[512:]
    out = bytearray(len(body))
    half = 8192
    for blk in range(0, len(body), 16384):
        block = body[blk:blk + 16384]
        for i in range(half):
            out[blk + 2 * i] = block[i]
            out[blk + 2 * i + 1] = block[half + i]
    return bytes(out)


def header_checksum(image: bytes) -> int:
    total = 0
    body = image[0x200:]
    if len(body) % 2:
        body += b"\x00"
    for (w,) in struct.iter_unpack(">H", body):
        total = (total + w) & 0xFFFF
    return total


def probe_rom(path) -> dict:
    p = Path(path)
    result = {
        "path": str(p), "filename": p.name, "ok": False,
        "needs_review": False, "reasons": [], "info": [],
        "title": "", "header_title_domestic": "", "header_title_overseas": "",
        "serial": "", "system": "", "regions": "", "io_support": "",
        "checksum_stored": None, "checksum_computed": None, "checksum_ok": None,
        "rom_range": "", "sram": "",
        "size": 0, "file_size": 0, "format": "plain",
        "crc32": None, "md5": "", "sha1": "", "sha256": "",
        "profile": None, "profile_title": "",
    }
    try:
        raw = p.read_bytes()
    except OSError as exc:
        result["reasons"].append(f"cannot read file: {exc}")
        return result
    result["file_size"] = len(raw)
    image = raw
    if p.suffix.lower() == ".smd" or is_smd_image(raw):
        if is_smd_image(raw):
            image = deinterleave_smd(raw)
            result["format"] = "smd (deinterleaved)"
            result["info"].append(
                "SMD copier image: 512-byte header stripped and 16 KiB "
                "blocks deinterleaved before hashing")
        else:
            result["reasons"].append(
                ".smd extension but no SMD copier header/interleave "
                "signature - treated as a plain image")
    result["size"] = len(image)
    result["crc32"] = zlib.crc32(image) & 0xFFFFFFFF
    result["md5"] = hashlib.md5(image).hexdigest()
    result["sha1"] = hashlib.sha1(image).hexdigest()
    result["sha256"] = hashlib.sha256(image).hexdigest()

    if len(image) < 0x200:
        result["reasons"].append(
            f"image is only {len(image)} bytes - too small for a Genesis header")
        result["needs_review"] = True
        return result

    result["system"] = _clean_title(image[0x100:0x110])
    result["header_title_domestic"] = _clean_title(image[0x120:0x150])
    result["header_title_overseas"] = _clean_title(image[0x150:0x180])
    result["serial"] = _clean_title(image[0x180:0x18E])
    result["io_support"] = _clean_title(image[0x190:0x1A0])
    result["regions"] = _clean_title(image[0x1F0:0x200])
    rom_start, rom_end = _u32be(image, 0x1A0), _u32be(image, 0x1A4)
    result["rom_range"] = f"0x{rom_start:06X}-0x{rom_end:06X}"
    result["checksum_stored"] = _u16be(image, 0x18E)
    result["checksum_computed"] = header_checksum(image)
    result["checksum_ok"] = result["checksum_stored"] == result["checksum_computed"]
    if image[0x1B0:0x1B2] == b"RA":
        sram_start, sram_end = _u32be(image, 0x1B4), _u32be(image, 0x1B8)
        result["sram"] = f"0x{sram_start:06X}-0x{sram_end:06X}"

    result["title"] = (result["header_title_overseas"]
                       or result["header_title_domestic"]
                       or p.stem)

    if not result["system"].startswith("SEGA"):
        result["reasons"].append(
            f"no SEGA system string at 0x100 (found {result['system']!r}) - "
            "not a Genesis/Mega Drive image")
    if not result["checksum_ok"]:
        result["info"].append(
            f"header checksum mismatch (stored 0x{result['checksum_stored']:04X}, "
            f"computed 0x{result['checksum_computed']:04X}) - common in hacks "
            "and translations; the runtime ignores it")
    if len(image) % 2:
        result["reasons"].append("odd image size (Genesis images are even-sized)")
    if len(image) > ROM_WINDOW_MAX:
        result["reasons"].append(
            f"image is {len(image)} bytes - larger than the runner's 4 MiB "
            "ROM window; titles this size need bank-switching support the "
            "current profiles do not provide")

    prof = profile_for_probe(result)
    if prof:
        result["profile"] = prof["id"]
        result["profile_title"] = prof["title"]
        result["ok"] = not result["reasons"]
        if result["reasons"]:
            result["needs_review"] = True
        return result

    # No profile matched: say exactly why, as specifically as possible.
    result["needs_review"] = True
    serial = result["serial"]
    for candidate in PROFILES.values():
        if serial.startswith(candidate["serial_hint"]):
            result["reasons"].append(
                f"{candidate['title']} serial ({serial}) but this is not the "
                f"qualified revision: the profile covers {candidate['revision']} "
                f"(CRC32 0x{candidate['crc32']:08X}, {candidate['size']} bytes); "
                f"this image is CRC32 0x{result['crc32']:08X}, {len(image)} bytes")
            break
    else:
        result["reasons"].append(
            "no integration profile for this game: a Genesis recomp needs "
            "per-game reverse-engineering data (RAM layout + function "
            "discovery from a disassembly) that cannot be derived from the "
            "ROM alone; the bundled profile covers Sonic the Hedgehog "
            "(JUE REV00)")
    result["ok"] = False
    return result


def probe_image_bytes(path) -> bytes:
    """The plain (deinterleaved) image bytes for a ROM file - what a
    conversion stages into the project."""
    raw = Path(path).read_bytes()
    if is_smd_image(raw):
        return deinterleave_smd(raw)
    return raw


# ---------------------------------------------------------------------------
# Cover art: libretro-thumbnails, Sega_-_Mega_Drive_-_Genesis set.
# Ported from the Recomp Covers tool (which proved this set needs the
# fuzzy listing fallback: Sega sets use legacy naming such as
# "Sonic The Hedgehog (USA, Europe)").
# ---------------------------------------------------------------------------

COVER_GH = "Sega_-_Mega_Drive_-_Genesis"
COVER_CANON = "Sega%20-%20Mega%20Drive%20-%20Genesis"
USER_AGENT = "GenesisRecompTools/1.0"
_ILLEGAL = dict.fromkeys(map(ord, '\\/:*?"<>|'), None)


def _lr_encode(name: str) -> str:
    return name.translate(_ILLEGAL).replace("&", "_")


def _http_get_once(url: str, timeout: int = 25):
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, None


def _http_get_retry(url: str, attempts: int = 3, timeout: int = 25):
    """Bytes or None. A 404 is definitive: never retried."""
    for i in range(attempts):
        data, status = _http_get_once(url, timeout=timeout)
        if data is not None:
            return data
        if status == 404:
            return None
        if i + 1 < attempts:
            time.sleep(2 * (i + 1))
    return None


def _valid_image(data) -> bool:
    if not data or len(data) < 4000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            return False
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w >= 100 and h >= 100
    return data[:3] == b"\xff\xd8\xff"


def _to_png(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def cover_variants(title: str):
    title = (title or "").strip()
    if not title:
        return []
    out = []

    def add(n):
        n = (n or "").strip()
        if n and n not in out:
            out.append(n)

    add(title)
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", title).strip()
    add(no_brackets)
    norm = re.sub(r"\((JUE|JU|UE|U|J)\)", "(World)", no_brackets)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    add(norm)
    core = title.split(" (")[0].strip()
    add(core)
    for base in (core, norm.split(" (")[0].strip()):
        for suffix in (" (USA)", " (USA, Europe)", " (World)",
                       " (USA, Europe, Brazil) (En)", " (Japan, USA)"):
            add(base + suffix)
    return out


def _cover_try_name(name: str, log=None):
    import urllib.parse
    enc = urllib.parse.quote(_lr_encode(name), safe="")
    cache = ART_CACHE / "genesis" / (enc + ".png")
    if cache.is_file():
        data = cache.read_bytes()
        if _valid_image(data):
            return data, f"cache ({name})"
    urls = [
        f"https://raw.githubusercontent.com/libretro-thumbnails/{COVER_GH}/master/Named_Boxarts/{enc}.png",
        f"https://thumbnails.libretro.com/{COVER_CANON}/Named_Boxarts/{enc}.png",
    ]
    for url in urls:
        data = _http_get_retry(url)
        if data and _valid_image(data):
            png = _to_png(data)
            if png:
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(png)
                except OSError:
                    pass
                host = "GitHub mirror" if "githubusercontent" in url else "libretro"
                return png, f"{host} ({name})"
    return None, None


_REGION_WORDS = {
    "usa", "europe", "japan", "world", "australia", "brazil", "korea",
    "canada", "france", "germany", "spain", "italy", "netherlands",
    "sweden", "denmark", "finland", "norway", "asia", "taiwan",
    "china", "russia", "mexico", "portugal", "greece", "india",
    "israel", "uk", "scandinavia", "latin america", "new zealand",
}


def _cover_listing(log=None):
    dest = ART_CACHE / "listings" / "genesis.json"
    stale = []
    try:
        blob = json.loads(dest.read_text())
        stale = blob.get("names", [])
        if time.time() - blob.get("ts", 0) < 7 * 24 * 3600 and stale:
            return stale
    except Exception:
        pass
    if not shutil.which("git"):
        return stale
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="genesis-list-"))
    try:
        repo = f"https://github.com/libretro-thumbnails/{COVER_GH}"
        subprocess.run(["git", "clone", "--quiet", "--depth", "1",
                        "--filter=tree:0", "--no-checkout", repo,
                        str(tmp / "repo")],
                       check=True, capture_output=True, timeout=300)
        out = subprocess.run(
            ["git", "-C", str(tmp / "repo"), "ls-tree", "-r",
             "--name-only", "HEAD"],
            check=True, capture_output=True, timeout=300).stdout
        names = [line[len("Named_Boxarts/"):-len(".png")]
                 for line in out.decode(errors="replace").splitlines()
                 if line.startswith("Named_Boxarts/")
                 and line.endswith(".png")]
        if names:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"ts": time.time(), "names": names}))
            return names
    except Exception as exc:
        log_line(log, f"cover: set listing failed ({exc})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return stale


def _regions_of(name: str):
    found = set()
    for group in re.findall(r"\(([^)]*)\)", name):
        for tok in re.split(r"[,+]", group):
            tok = tok.strip().lower()
            if tok in _REGION_WORDS:
                found.add(tok)
    return found


def fetch_cover(title: str, log=None):
    """(png_bytes, desc) or (None, None): exact variants first, then a
    fuzzy match against the set's real file listing."""
    for name in cover_variants(title):
        data, desc = _cover_try_name(name, log)
        if data:
            return data, desc
    core = _lr_encode(title).split(" (")[0].strip().lower()
    want = _regions_of(title)
    scored, partial = [], []
    for n in _cover_listing(log):
        if _lr_encode(n).split(" (")[0].strip().lower() != core:
            continue
        regs = _regions_of(n)
        low = n.lower()
        score = 100 - 4 * len(regs - want) - len(n)
        if any(t in low for t in ("(proto)", "(beta)", "[hack", "(sample)",
                                  "(demo)", "(unl)")):
            score -= 25
        if want and not want.issubset(regs):
            partial.append((score + 12 * len(want & regs), n))
            continue
        scored.append((score, n))
    pool = scored or partial
    pool.sort(key=lambda t: -t[0])
    for _, cand in pool[:6]:
        data, desc = _cover_try_name(cand, log)
        if data:
            return data, f"listing match ({cand})"
    return None, None


def png_to_tga(png: bytes):
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        out = io.BytesIO()
        im.save(out, "TGA")
        return out.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Windows build support files (app-owned, self-provisioning from
# birth — the GB tool's field lesson: never depend on a folder the
# user happens to have). The Genesis runner needs only SDL2 for its
# MinGW build: system OpenGL, no zlib, no Mesa (unlike GB).
# ---------------------------------------------------------------------------

WIN_SDL2_DIR = TOOLS_DIR / "SDL2-2.30.0"
WIN_PROVISIONED_FILES = [
    ("SDL2 MinGW config (sdl2-config.cmake)",
     WIN_SDL2_DIR / "x86_64-w64-mingw32/lib/cmake/SDL2/sdl2-config.cmake"),
    ("SDL2.dll (MinGW runtime)",
     WIN_SDL2_DIR / "x86_64-w64-mingw32/bin/SDL2.dll"),
]
WIN_DOWNLOADS = [
    ("SDL2-devel-2.30.0-mingw.tar.gz",
     "https://github.com/libsdl-org/SDL/releases/download/release-2.30.0/SDL2-devel-2.30.0-mingw.tar.gz",
     "dir"),
]


def provisioned_ok() -> bool:
    return all(p.is_file() and p.stat().st_size > 0
               for _, p in WIN_PROVISIONED_FILES)


def _download_atomic(url: str, dest: Path, log=None) -> None:
    tmp = dest.with_name(dest.name + ".part")
    log_line(log, f"Downloading {url}")
    curl = shutil.which("curl")
    try:
        if curl:
            proc = subprocess.run(
                [curl, "-fL", "--retry", "3", "-o", str(tmp), url],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if proc.stdout:
                for line in proc.stdout.splitlines():
                    log_line(log, line)
            if proc.returncode != 0:
                raise GenesisError(f"curl failed ({proc.returncode}) for {url}")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def ensure_win_tools(log=None) -> None:
    if provisioned_ok():
        return
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    downloads = TOOLS_DIR / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    import tarfile
    for fname, url, kind in WIN_DOWNLOADS:
        marker = TOOLS_DIR / Path(fname).stem.replace(
            "-devel-2.30.0-mingw.tar", "")
        archive = downloads / fname
        if not archive.is_file():
            _download_atomic(url, archive, log)
        log_line(log, f"Extracting {fname}")
        with tarfile.open(archive) as tf:
            tf.extractall(TOOLS_DIR)
    if not provisioned_ok():
        missing = [label for label, p in WIN_PROVISIONED_FILES if not p.is_file()]
        raise GenesisError(
            "Windows support files are incomplete after provisioning: "
            + ", ".join(missing))


# ---------------------------------------------------------------------------
# Framework template: the productised Sonic game project (framework +
# recomp-ui pinned inside it as submodules), plus a host build of the
# GenesisRecomp recompiler (toolbox evidence + the Windows build's
# code generator).
# ---------------------------------------------------------------------------

def _template_markers(t: Path) -> bool:
    return all([
        (t / "CMakeLists.txt").is_file(),
        (t / "game" / "game.toml").is_file(),
        (t / "game" / "sonic1_spec.c").is_file(),
        (t / "boxart.tga").is_file(),
        (t / "segagenesisrecomp" / "recompiler" / "CMakeLists.txt").is_file(),
        (t / "segagenesisrecomp" / "runner" / "main.c").is_file(),
        (t / "segagenesisrecomp" / "external" / "m68k-recomp-core").is_dir(),
        (t / "recomp-ui" / "recomp_ui.cmake").is_file(),
    ])


def template_toast_capable(t: Path) -> bool:
    header = t / "recomp-ui" / "src" / "recomp_runtime_ui.h"
    try:
        return "recomp_runtime_ui_set_toast" in header.read_text(errors="replace")
    except OSError:
        return False


def find_template():
    env = os.environ.get("GENESIS_TEMPLATE_DIR")
    if env and _template_markers(Path(env)):
        return Path(env)
    cached = FRAMEWORK_CACHE / TEMPLATE_DIRNAME
    if _template_markers(cached):
        return cached
    dev = Path.home() / "workspace" / "genesis-probe" / TEMPLATE_DIRNAME
    if _template_markers(dev):
        return dev
    return None


def ensure_frameworks(log=None) -> dict:
    """Returns {"template": Path, "host_recompiler": Path|None,
    "toast": bool}. Clones the template (recursive submodules) into
    the app's framework-cache on first run, bumps its recomp-ui
    submodule to the toast-capable pin when needed, and builds the
    host GenesisRecomp once."""
    template = find_template()
    if template is None:
        FRAMEWORK_CACHE.mkdir(parents=True, exist_ok=True)
        dest = FRAMEWORK_CACHE / TEMPLATE_DIRNAME
        if dest.exists():
            shutil.rmtree(dest)
        run(["git", "clone", "--recursive", SONIC_REPO_URL, str(dest)], log=log)
        template = dest
    if not _template_markers(template):
        raise GenesisError(
            f"framework template at {template} is incomplete "
            "(missing project/framework files) - delete it and re-run "
            "so the app can fetch a fresh copy")

    toast = template_toast_capable(template)
    if not toast and (template / "recomp-ui" / ".git").exists():
        try:
            log_line(log, "recomp-ui pin predates the toast API; moving "
                          "it to the current upstream pin "
                          f"({RECOMP_UI_TOAST_PIN[:8]})")
            run(["git", "-C", str(template / "recomp-ui"), "fetch",
                 "origin"], log=log)
            run(["git", "-C", str(template / "recomp-ui"), "checkout",
                 RECOMP_UI_TOAST_PIN], log=log)
            toast = template_toast_capable(template)
        except GenesisError as exc:
            log_line(log, f"recomp-ui pin move failed ({exc}); "
                          "RA pop-ups will be log-only in builds")
    log_line(log, f"recomp-ui toast API: {'available' if toast else 'unavailable'}")

    host_recompiler = FRAMEWORK_CACHE / "host-build" / "GenesisRecomp"
    if not host_recompiler.is_file():
        fw = template / "segagenesisrecomp"
        bdir = FRAMEWORK_CACHE / "host-build"
        run(["cmake", "-S", str(fw / "recompiler"), "-B", str(bdir),
             "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"], log=log)
        run(["cmake", "--build", str(bdir), "--target", "GenesisRecomp",
             "-j", "2"], log=log)
    if not host_recompiler.is_file():
        log_line(log, "warning: host GenesisRecomp was not produced")
        host_recompiler = None
    return {"template": template, "host_recompiler": host_recompiler,
            "toast": toast}


# ---------------------------------------------------------------------------
# Scaffold: stage the template project, drop in the ROM, wire RA
# ---------------------------------------------------------------------------

_ORIG_SUFFIX = ".genesis-orig"
_STAGE_MARKER = ".genesis-tools-stage.json"


def _backup_once(path: Path) -> Path:
    orig = path.with_name(path.name + _ORIG_SUFFIX)
    if not orig.exists():
        shutil.copy2(path, orig)
    return orig


def _restore_orig(path: Path) -> None:
    orig = path.with_name(path.name + _ORIG_SUFFIX)
    if orig.exists():
        shutil.copy2(orig, path)
        orig.unlink()


def _template_fingerprint(template: Path, toast: bool) -> str:
    h = hashlib.sha256()
    for rel in ("CMakeLists.txt", "game/sonic1_spec.c", "game/game.toml",
                "segagenesisrecomp/runner/main.c"):
        try:
            h.update((template / rel).read_bytes())
        except OSError:
            pass
    h.update(b"toast" if toast else b"notoast")
    return h.hexdigest()[:16]


def _stage_template(template: Path, proj: Path, log=None) -> None:
    if proj.exists():
        shutil.rmtree(proj)
    skip_names = {".git", "__pycache__"}

    def ignore(dirpath, names):
        out = []
        for n in names:
            full = Path(dirpath) / n
            rel = full.relative_to(template).as_posix()
            if n in skip_names:
                out.append(n)
            elif n.startswith("build") and full.is_dir():
                out.append(n)
            elif rel in ("game/sonic.bin", "release-linux"):
                out.append(n)
            elif n.endswith(".AppImage") or n.endswith(_ORIG_SUFFIX):
                out.append(n)
        return out

    shutil.copytree(template, proj, ignore=ignore)
    log_line(log, f"Staged template project at {proj}")


def _patch_spec(spec_path: Path, log=None) -> None:
    """Point the game spec's on_frame_post at the RA module and make
    its header visible. Loud failure if the anchors are gone (an
    upstream update moved them) - never a silent no-RA build."""
    _backup_once(spec_path)
    text = spec_path.read_text()
    if '#include "genesis_ra.h"' not in text:
        anchor = '#include "sonic1_audio.h"'
        if anchor not in text:
            raise GenesisError(
                f"RA patch: include anchor not found in {spec_path.name} "
                "(upstream layout changed?)")
        text = text.replace(
            anchor, anchor + '\n#ifdef GENESIS_RA\n#include "genesis_ra.h"\n#endif', 1)
    if "genesis_ra_frame_post" not in text:
        new_text, n = re.subn(r"\.on_frame_post\s*=\s*NULL",
                              ".on_frame_post          = genesis_ra_frame_post",
                              text, count=1)
        if n == 0:
            raise GenesisError(
                f"RA patch: .on_frame_post anchor not found in "
                f"{spec_path.name} (upstream layout changed?)")
        text = new_text
    spec_path.write_text(text)
    log_line(log, "RA: spec on_frame_post wired to genesis_ra_frame_post")


def _patch_runner_main(main_path: Path, log=None) -> None:
    """Hand the runtime UI to the RA module right after the runner
    creates it. Marker-guarded and idempotent."""
    _backup_once(main_path)
    text = main_path.read_text()
    if '#include "genesis_ra.h"' not in text:
        anchor = '#include "recomp_runtime_ui.h"'
        if anchor not in text:
            raise GenesisError(
                "RA patch: recomp_runtime_ui.h include anchor not found "
                "in runner/main.c (upstream layout changed?)")
        text = text.replace(
            anchor, anchor + '\n#ifdef GENESIS_RA\n#include "genesis_ra.h"\n#endif', 1)
    if "genesis_ra_bind_ui" not in text:
        anchor = "s_runtime_ui.ui = recomp_runtime_ui_create_standard(&cfg);"
        if anchor not in text:
            raise GenesisError(
                "RA patch: runtime-UI creation anchor not found in "
                "runner/main.c (upstream layout changed?)")
        text = text.replace(
            anchor, anchor + "\n#ifdef GENESIS_RA\n"
            "        genesis_ra_bind_ui(s_runtime_ui.ui);\n#endif", 1)
    main_path.write_text(text)
    log_line(log, "RA: runner runtime-UI bound to the RA module")


RA_CMAKE_BEGIN = "# === GenesisRecomp Tools: RetroAchievements (begin) ==="
RA_CMAKE_END = "# === GenesisRecomp Tools: RetroAchievements (end) ==="


def _ra_cmake_block(prof: dict, toast: bool, popups: bool) -> str:
    tgt = prof["target"]
    libs = prof.get("lib_targets", [])
    lines = [
        RA_CMAKE_BEGIN,
        'if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/ra/genesis_ra.cpp")',
        '  file(GLOB_RECURSE GENESIS_RA_RCHEEVOS_SOURCES',
        '       "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/src/*.c")',
        '  list(FILTER GENESIS_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")',
        '  list(FILTER GENESIS_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")',
        f'  target_sources({tgt} PRIVATE',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/genesis_ra.cpp"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra_http.c"',
        '    ${GENESIS_RA_RCHEEVOS_SOURCES})',
        f'  target_include_directories({tgt} PRIVATE',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra"',
        '    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/include")',
        f'  target_compile_definitions({tgt} PRIVATE GENESIS_RA=1)',
    ]
    if toast:
        lines.append(
            f'  target_compile_definitions({tgt} PRIVATE GENESIS_RA_HAVE_TOAST=1)')
    if not popups:
        lines.append(
            f'  target_compile_definitions({tgt} PRIVATE RA_DISABLE_POPUPS=1)')
    for lib in libs:
        lines += [
            f'  if(TARGET {lib})',
            f'    target_include_directories({lib} PRIVATE',
            '      "${CMAKE_CURRENT_SOURCE_DIR}/ra")',
            f'    target_compile_definitions({lib} PRIVATE GENESIS_RA=1)',
            '  endif()',
        ]
    lines += [
        '  find_package(Threads REQUIRED)',
        f'  target_link_libraries({tgt} PRIVATE Threads::Threads)',
        '  if(WIN32)',
        f'    target_link_libraries({tgt} PRIVATE winhttp)',
        '  else()',
        '    find_package(OpenSSL REQUIRED)',
        f'    target_link_libraries({tgt} PRIVATE OpenSSL::SSL OpenSSL::Crypto)',
        '  endif()',
        'endif()',
        RA_CMAKE_END,
        '',
    ]
    return "\n".join(lines)


def _apply_ra(proj: Path, prof: dict, toast: bool, popups: bool, log=None) -> None:
    ra_dir = proj / "ra"
    if ra_dir.exists():
        shutil.rmtree(ra_dir)
    shutil.copytree(BUNDLE_RA, ra_dir)
    _patch_spec(proj / prof["spec_rel"], log)
    _patch_runner_main(proj / "segagenesisrecomp" / "runner" / "main.c", log)
    cmakelists = proj / "CMakeLists.txt"
    _backup_once(cmakelists)
    text = cmakelists.read_text()
    if RA_CMAKE_BEGIN in text:
        text = re.sub(re.escape(RA_CMAKE_BEGIN) + r".*?" +
                      re.escape(RA_CMAKE_END) + r"\n?", "",
                      text, flags=re.S)
    cmakelists.write_text(text.rstrip() + "\n\n" +
                          _ra_cmake_block(prof, toast, popups))


def _strip_ra(proj: Path, prof: dict) -> None:
    ra_dir = proj / "ra"
    if ra_dir.exists():
        shutil.rmtree(ra_dir)
    for rel in (prof["spec_rel"], "segagenesisrecomp/runner/main.c",
                "CMakeLists.txt"):
        _restore_orig(proj / rel)


WINMAIN_SHIM_C = """/* GenesisRecomp Tools: MinGW entry shim.
 *
 * The project links SDL2main on Windows; its WinMain calls SDL_main.
 * The runner provides a plain main() (it defines SDL_MAIN_HANDLED),
 * which MSVC's SDL2main accommodates but MinGW's does not - the link
 * fails with an undefined reference to SDL_main. Forward it. The
 * __MINGW32__ guard keeps this a no-op for upstream's MSVC builds.
 */
#if defined(_WIN32) && defined(__MINGW32__)
extern int main(int argc, char *argv[]);
int SDL_main(int argc, char *argv[]) { return main(argc, argv); }
#endif
"""

WINMAIN_CMAKE_BEGIN = "# === GenesisRecomp Tools: MinGW entry shim (begin) ==="
WINMAIN_CMAKE_END = "# === GenesisRecomp Tools: MinGW entry shim (end) ==="


def _apply_winmain_shim(proj: Path, prof: dict, log=None) -> None:
    (proj / "genesis_winmain_shim.c").write_text(WINMAIN_SHIM_C)
    cmakelists = proj / "CMakeLists.txt"
    text = cmakelists.read_text()
    if WINMAIN_CMAKE_BEGIN not in text:
        block = (f"{WINMAIN_CMAKE_BEGIN}\n"
                 f"if(WIN32)\n"
                 f"  target_sources({prof['target']} PRIVATE "
                 f"\"${{CMAKE_CURRENT_SOURCE_DIR}}/genesis_winmain_shim.c\")\n"
                 f"endif()\n"
                 f"{WINMAIN_CMAKE_END}\n")
        cmakelists.write_text(text.rstrip() + "\n\n" + block)
        log_line(log, "build fix: MinGW SDL_main entry shim added")


def _patch_gcc_ppnumber(proj: Path, log=None) -> None:
    """Upstream's game sources contain a hex literal ending in E
    directly followed by an operator (0xFE5E+oscillators[...]).
    C's preprocessing-number token absorbs the sign after E, so GCC
    rejects it ("invalid suffix on integer constant") where Clang /
    MSVC tokenize leniently. Inserting a space is semantics-preserving
    and lets the stock GCC toolchain build the staged copy. Applied
    to the staged game sources only, on every scaffold (idempotent:
    the pattern no longer matches once spaced)."""
    pat = re.compile(r"0x([0-9A-Fa-f]*[eE])([+-])(?=[A-Za-z_(])")
    for src in sorted((proj / "game").glob("*.c")) + \
            sorted((proj / "game").glob("*.h")):
        try:
            text = src.read_text(errors="replace")
        except OSError:
            continue
        new_text, n = pat.subn(r"0x\1 \2", text)
        if n:
            src.write_text(new_text)
            log_line(log, f"build fix: spaced {n} hex-literal "
                          f"operator(s) in game/{src.name} for GCC")


def scaffold_project(job: dict, probe: dict, template: Path,
                     toast: bool, log=None) -> Path:
    prof = PROFILES[probe["profile"]]
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    proj = out_dir / f"{prof['camel']}-src"
    ra_cfg = job.get("ra", {}) or {}
    ra_on = bool(ra_cfg.get("enabled", True))
    fingerprint = _template_fingerprint(template, toast)
    want = {"profile": prof["id"], "crc32": probe["crc32"],
            "ra": ra_on, "popups": bool(ra_cfg.get("popups", True)),
            "fingerprint": fingerprint}
    have = None
    marker = proj / _STAGE_MARKER
    if marker.is_file():
        try:
            have = json.loads(marker.read_text())
        except Exception:
            have = None
    if have != want or not (proj / "CMakeLists.txt").is_file():
        _stage_template(template, proj, log)
    else:
        log_line(log, f"Reusing staged project at {proj}")

    _patch_gcc_ppnumber(proj, log)

    # The ROM the build consumes (deinterleaved image for .smd).
    rom_dest = proj / prof["rom_dest"]
    rom_dest.parent.mkdir(parents=True, exist_ok=True)
    rom_dest.write_bytes(probe_image_bytes(probe["path"]))

    # Cover art -> the launcher's boxart.tga (the project ships its
    # own art as fallback; a fetched cover replaces it).
    png, desc = fetch_cover(prof["title"], log)
    if png:
        tga = png_to_tga(png)
        if tga:
            boxart = proj / "boxart.tga"
            _backup_once(boxart)
            boxart.write_bytes(tga)
            log_line(log, f"Cover art: {desc} -> boxart.tga")
        art_dir = ART_CACHE / "package-art"
        art_dir.mkdir(parents=True, exist_ok=True)
        (art_dir / f"{prof['camel']}.png").write_bytes(png)
    else:
        log_line(log, "Cover art: none found online; the project's "
                      "bundled box art is used")

    if ra_on:
        _apply_ra(proj, prof, toast, bool(ra_cfg.get("popups", True)), log)
        if not toast:
            log_line(log, "RA note: recomp-ui toast API unavailable - "
                          "pop-ups will be log-only in this build")
    else:
        _strip_ra(proj, prof)
    _apply_winmain_shim(proj, prof, log)
    marker.write_text(json.dumps(want, indent=1))
    return proj


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _cmake_env() -> dict:
    """Subprocess environment for CMake/Ninja. PATH is rebuilt to
    exclude the app tools dir: SDL2's MinGW files living there have
    poisoned Linux SDL2 discovery in sibling tools."""
    env = dict(os.environ)
    parts = []
    tools = str(TOOLS_DIR)
    for part in env.get("PATH", "").split(os.pathsep):
        if part and os.path.abspath(part) != os.path.abspath(tools):
            parts.append(part)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _sdl2_cmake_dir() -> str:
    candidates = sorted(glob.glob("/usr/lib/*/cmake/SDL2")) + \
        sorted(glob.glob("/usr/lib/cmake/SDL2")) + \
        sorted(glob.glob("/usr/local/lib/cmake/SDL2"))
    for c in candidates:
        if (Path(c) / "sdl2-config.cmake").is_file() or \
                (Path(c) / "SDL2Config.cmake").is_file():
            return c
    return ""


def _write_mingw_toolchain(proj: Path, host_recompiler) -> Path:
    tc = proj / "tc-mingw.cmake"
    sdl2_cmake = (WIN_SDL2_DIR / "x86_64-w64-mingw32" / "lib" / "cmake"
                  / "SDL2").as_posix()
    lines = [
        "set(CMAKE_SYSTEM_NAME Windows)",
        "set(CMAKE_SYSTEM_PROCESSOR x86_64)",
        "set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)",
        "set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)",
        "set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)",
        f"set(CMAKE_FIND_ROOT_PATH {TOOLS_DIR.as_posix()})",
        "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)",
        "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)",
        "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)",
        f'set(SDL2_DIR "{sdl2_cmake}")',
    ]
    if host_recompiler:
        lines += [
            "# GenesisRecomp Tools: generation must run on the host -",
            "# use the host-built recompiler instead of building a",
            "# Windows one that cannot run during this build.",
            "if(NOT TARGET GenesisRecomp)",
            "  add_executable(GenesisRecomp IMPORTED GLOBAL)",
            f'  set_target_properties(GenesisRecomp PROPERTIES '
            f'IMPORTED_LOCATION "{Path(host_recompiler).as_posix()}")',
            "endif()",
        ]
    tc.write_text("\n".join(lines) + "\n")
    return tc


def build_target(job: dict, proj: Path, target_os: str,
                 host_recompiler, log=None) -> Path:
    prof = PROFILES[job["_profile_id"]]
    jobs = str(job.get("jobs", 2))
    env = _cmake_env()
    if target_os == "linux":
        bdir = proj / "build-linux"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DGEN_ENABLE_TRACE=OFF", "-DGEN_DEV_TRACE=OFF",
               "-DSONIC_REVERSE_DEBUG=OFF"]
        sdl2_dir = _sdl2_cmake_dir()
        if sdl2_dir:
            cmd.append(f"-DSDL2_DIR={sdl2_dir}")
        run(cmd, env=env, log=log)
        run(["cmake", "--build", str(bdir), "--target", prof["target"],
             "-j", jobs], env=env, log=log)
        exe = bdir / prof["exe"]
    else:
        ensure_win_tools(log)
        tc = _write_mingw_toolchain(proj, host_recompiler)
        bdir = proj / "build-win"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               f"-DCMAKE_TOOLCHAIN_FILE={tc}",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DGEN_ENABLE_TRACE=OFF", "-DGEN_DEV_TRACE=OFF",
               "-DSONIC_REVERSE_DEBUG=OFF"]
        # Some runner headers include <SDL2/SDL.h> (parent-prefixed);
        # the SDL2 CMake package only exposes the inner include dir
        # (for <SDL.h>), so add the parent include root as well.
        sdl2_inc = (WIN_SDL2_DIR / "x86_64-w64-mingw32"
                    / "include").as_posix()
        cmd += [f"-DCMAKE_C_FLAGS=-I{sdl2_inc}",
                f"-DCMAKE_CXX_FLAGS=-I{sdl2_inc}"]
        run(cmd, env=env, log=log)
        run(["cmake", "--build", str(bdir), "--target", prof["target"],
             "-j", jobs], env=env, log=log)
        exe = bdir / (prof["exe"] + ".exe")
    if not exe.is_file() or exe.stat().st_size < 1_000_000:
        raise GenesisError(
            f"{target_os} build finished but {exe.name} is missing or "
            f"suspiciously small - treating as failed")
    return exe


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

PACKAGE_README = """{title} ({revision}) - native PC recompilation (GenesisRecomp Tools)

Built with GenesisRecomp Tools from the player's own {rom_name} image
(CRC32 {crc32:08X}). This package contains NO ROM and NO BIOS - Genesis
cartridges need no BIOS. At launch, pick your own dump of the game in
the launcher; it verifies the image's CRC before starting, and only
the qualified revision ({revision}) is accepted.

Run:
  Linux:   ./{exe}            (or pass the ROM path as an argument)
  Windows: {exe}.exe

Controls and display options are in the launcher and the in-game
menu (Esc). Saves are written next to the executable.
{ra_block}
Credits: segagenesisrecomp framework by Matthew Stanley (mstan),
RetroPortingToolkit - PolyForm Noncommercial 1.0.0. recomp-ui by the
same team (MIT). GenesisRecomp Tools is an unofficial fan tool; it is
not affiliated with SEGA. Sonic the Hedgehog is a trademark of SEGA.
"""

RA_PACKAGE_BLOCK = """
RetroAchievements: enabled. retroachievements.toml sits next to the
executable with your username and Connect token; achievements pop up
in game on unlock, with a login toast at start. Note: RetroAchieve-
ments counts unlocks from recomp clients as Casual (softcore) - the
client is an unapproved emulator to the service, and that cannot be
changed without spoofing another client, which this tool never does.
"""


def package_built(job: dict, probe: dict, proj: Path, target_os: str,
                  exe: Path, log=None) -> Path:
    prof = PROFILES[probe["profile"]]
    out_dir = Path(job["output_dir"])
    ra_cfg = job.get("ra", {}) or {}
    ra_on = bool(ra_cfg.get("enabled", True))
    suffix = "Linux" if target_os == "linux" else "Windows"
    stage = out_dir / ".genesis-packages" / f"{prof['camel']}-{target_os}"
    if stage.exists():
        shutil.rmtree(stage)
    payload = stage / f"{prof['title']} (Genesis)"
    payload.mkdir(parents=True)
    shutil.copy2(exe, payload / exe.name)

    assets = exe.parent / "assets"
    if assets.is_dir():
        shutil.copytree(assets, payload / "assets")
    else:
        raise GenesisError(
            "launcher assets/ were not staged next to the executable - "
            "the package would boot without its launcher")

    if target_os == "windows":
        dlls = [exe.parent / "SDL2.dll"]
        mingw_bin = Path("/usr/x86_64-w64-mingw32/bin")
        for name in ("libstdc++-6.dll", "libgcc_s_seh-1.dll",
                     "libwinpthread-1.dll"):
            dlls.append(mingw_bin / name)
        if not dlls[0].is_file():
            dlls[0] = (WIN_SDL2_DIR / "x86_64-w64-mingw32" / "bin"
                       / "SDL2.dll")
        for dll in dlls:
            if dll.is_file():
                dest = payload / dll.name
                shutil.copy2(dll, dest)
                if dll.parent == mingw_bin:
                    run(["x86_64-w64-mingw32-strip", str(dest)], log=log)

    ra_block = ""
    if ra_on:
        example = BUNDLE_RA / "retroachievements.toml.example"
        if example.is_file():
            shutil.copy2(example, payload / "retroachievements.toml.example")
        toml_src = ra_cfg.get("toml") or ""
        if toml_src and Path(toml_src).is_file():
            shutil.copy2(toml_src, payload / "retroachievements.toml")
            ra_block = RA_PACKAGE_BLOCK
        else:
            ra_block = ("\nRetroAchievements: compiled in. To enable, "
                        "copy retroachievements.toml.example to\n"
                        "retroachievements.toml next to the executable "
                        "and fill in your username\nand Connect token.\n")

    (payload / "README.txt").write_text(PACKAGE_README.format(
        title=prof["title"], revision=prof["revision"],
        rom_name=Path(probe["path"]).name, crc32=probe["crc32"],
        exe=prof["exe"], ra_block=ra_block))

    art = ART_CACHE / "package-art" / f"{prof['camel']}.png"
    if art.is_file():
        adir = payload / "artwork" / prof["title"]
        adir.mkdir(parents=True)
        shutil.copy2(art, adir / "front.png")

    zip_path = out_dir / f"{prof['camel']}-{suffix}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(stage).as_posix())
    log_line(log, f"Packaged {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def run_job(job: dict, log=None) -> dict:
    roms = job.get("roms") or []
    if not roms:
        raise GenesisError("job has no ROMs")
    targets = [t for t, on in (job.get("targets") or {}).items() if on]
    if not targets:
        targets = ["linux"]
    fw = ensure_frameworks(log)
    template, toast = fw["template"], fw["toast"]
    summary = {"packages": [], "errors": []}
    for rom in roms:
        probe = probe_rom(rom)
        name = Path(rom).name
        if probe["profile"] is None:
            reasons = "; ".join(probe["reasons"]) or "no profile"
            raise GenesisError(f"{name}: cannot convert - {reasons}")
        if probe["reasons"] and not job.get("force"):
            raise GenesisError(
                f"{name}: probe flagged this image "
                f"({'; '.join(probe['reasons'])}) - fix the image or "
                "re-run with force")
        log_line(log, f"{name}: profile {probe['profile']} "
                      f"({probe['profile_title']}, CRC32 "
                      f"0x{probe['crc32']:08X})")
        job["_profile_id"] = probe["profile"]
        proj = scaffold_project(job, probe, template, toast, log)
        failed_targets = []
        for target_os in targets:
            try:
                exe = build_target(job, proj, target_os,
                                   fw["host_recompiler"], log)
            except Exception as exc:
                failed_targets.append(target_os)
                log_line(log, f"{target_os} build FAILED: {exc}")
                summary["errors"].append(f"{name} [{target_os}]: {exc}")
                continue
            zip_path = package_built(job, probe, proj, target_os, exe, log)
            entry = {"rom": rom, "target": target_os,
                     "zip": str(zip_path)}
            if failed_targets:
                entry["note"] = ("partial: failed targets: "
                                  + ", ".join(failed_targets))
            summary["packages"].append(entry)
        if len(failed_targets) == len(targets):
            raise GenesisError(
                f"{name}: all target builds failed "
                f"({'; '.join(summary['errors'])})")
    return summary


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------

def _check(ok, label, detail, fix=None):
    return {"ok": bool(ok), "label": label, "detail": detail, "fix": fix}


def toolbox_status() -> list:
    import importlib.util
    checks = []
    py_ok = sys.version_info >= (3, 10)
    checks.append(_check(py_ok, "Python 3.10+", sys.version.split()[0],
                         "Install Python 3.10 or newer"))
    for mod, label in (("PySide6", "PySide6 (GUI)"),
                       ("PIL", "Pillow (cover art)")):
        checks.append(_check(importlib.util.find_spec(mod) is not None,
                             label, "importable" if
                             importlib.util.find_spec(mod) else "missing",
                             "pip install -r requirements.txt"))
    for exe, label, fix in (
            ("cmake", "CMake", "sudo apt install -y cmake"),
            ("ninja", "Ninja", "sudo apt install -y ninja-build"),
            ("gcc", "GCC (host C)", "sudo apt install -y build-essential"),
            ("g++", "G++ (host C++)", "sudo apt install -y build-essential"),
            ("git", "git", "sudo apt install -y git"),
            ("curl", "curl", "sudo apt install -y curl"),
            ("x86_64-w64-mingw32-g++", "MinGW-w64 G++ (Windows builds)",
             "sudo apt install -y g++-mingw-w64-x86-64"),
            ("x86_64-w64-mingw32-strip", "MinGW strip",
             "sudo apt install -y binutils-mingw-w64-x86-64")):
        path = shutil.which(exe)
        detail = ""
        if path:
            try:
                out = subprocess.run([exe, "--version"], text=True,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=10)
                detail = (out.stdout or "").splitlines()[0][:60]
            except Exception:
                detail = path
        checks.append(_check(path is not None, label,
                             detail or "not found", fix))
    sdl2 = subprocess.run(["pkg-config", "--modversion", "sdl2"],
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    checks.append(_check(sdl2.returncode == 0, "SDL2 development files",
                         (sdl2.stdout or "").strip() or "pkg-config failed",
                         "sudo apt install -y libsdl2-dev"))
    checks.append(_check(Path("/usr/include/openssl/ssl.h").is_file(),
                         "OpenSSL headers (RetroAchievements)",
                         "present" if Path("/usr/include/openssl/ssl.h").is_file()
                         else "missing",
                         "sudo apt install -y libssl-dev"))
    xcb = subprocess.run(["dpkg", "-s", "libxcb-cursor0"], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    checks.append(_check(xcb.returncode == 0,
                         "libxcb-cursor0 (Qt GUI on minimal systems)",
                         "installed" if xcb.returncode == 0 else "missing",
                         "sudo apt install -y libxcb-cursor0"))
    checks.append(_check(provisioned_ok(),
                         "Windows support files (SDL2 MinGW kit, app-owned)",
                         "provisioned" if provisioned_ok() else
                         "downloaded automatically on the first Windows build",
                         None))
    template = find_template()
    checks.append(_check(template is not None,
                         "Framework template (SonicTheHedgehogRecomp)",
                         str(template) if template else
                         "fetched automatically on first conversion",
                         None))
    if template is not None:
        checks.append(_check(
            template_toast_capable(template),
            "recomp-ui toast API (RA pop-ups)",
            "available" if template_toast_capable(template) else
            "pin predates it - pop-ups log-only until the pin moves",
            None))
    host = FRAMEWORK_CACHE / "host-build" / "GenesisRecomp"
    checks.append(_check(host.is_file(), "Host GenesisRecomp recompiler",
                         "built" if host.is_file() else
                         "built automatically on first conversion",
                         None))
    return checks


INSTALL_PACKAGES = [
    "build-essential", "cmake", "ninja-build", "git", "curl",
    "pkg-config", "libsdl2-dev", "libssl-dev",
    "g++-mingw-w64-x86-64", "binutils-mingw-w64-x86-64",
    "libxcb-cursor0", "python3-venv",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="genesis-engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("toolbox")
    pp = sub.add_parser("probe")
    pp.add_argument("--rom", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--job", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "toolbox":
        for c in toolbox_status():
            mark = "OK " if c["ok"] else "MISS"
            print(f"[{mark}] {c['label']}: {c['detail']}")
        return 0
    if args.cmd == "probe":
        print(json.dumps(probe_rom(args.rom), indent=1))
        return 0
    if args.cmd == "run":
        job = json.loads(Path(args.job).read_text())
        try:
            summary = run_job(job)
        except GenesisError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
