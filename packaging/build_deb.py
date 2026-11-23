#!/usr/bin/env python3
"""Build the Card Pi Beta Debian package using the standard library."""

from io import BytesIO
import argparse
from pathlib import Path
from runpy import run_path
from tarfile import DIRTYPE, TarInfo, USTAR_FORMAT, open as tar_open
from tempfile import TemporaryDirectory


PACKAGE = "card-pi"
SOURCE = Path(__file__).resolve().parent
VERSION = run_path(str(SOURCE.parent / "card_pi/version.py"))["VERSION"]


def write_tar(path, files):
    with tar_open(path, "w:gz", format=USTAR_FORMAT) as archive:
        directories = set()
        for name, (data, mode) in files.items():
            for parent in reversed(Path(name).parents):
                if parent == Path(".") or parent in directories:
                    continue
                directory = TarInfo(f"./{parent.as_posix()}")
                directory.type = DIRTYPE
                directory.mode = 0o755
                directory.uname = directory.gname = "root"
                archive.addfile(directory)
                directories.add(parent)
            entry = TarInfo(f"./{name}")
            entry.size = len(data)
            entry.mode = mode
            entry.uname = entry.gname = "root"
            archive.addfile(entry, BytesIO(data))


def write_deb(path, members):
    # Debian uses the common ar format with short member names and no symbol table.
    with path.open("wb") as archive:
        archive.write(b"!<arch>\n")
        for member in members:
            data = member.read_bytes()
            header = f"{member.name:16}{0:<12}{0:<6}{0:<6}{'100644':8}{len(data):<10}`\n"
            archive.write(header.encode("ascii"))
            archive.write(data)
            if len(data) % 2:
                archive.write(b"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SOURCE.parent / "dist")
    output_dir = parser.parse_args().output_dir
    control = f"""Package: {PACKAGE}
Version: {VERSION}
Architecture: arm64
Section: utils
Priority: optional
Maintainer: Card Pi developers <card-pi@localhost>
Depends: python3 (>= 3.11), python3-pil, python3-pyte, python3-markdown-it, fonts-dejavu-mono | fonts-dejavu-core, ca-certificates, curl, fonts-noto-cjk, librime1t64 | librime1, rime-prelude, rime-essay, rime-data-pinyin-simp, rime-data-luna-pinyin, rime-data-double-pinyin
Description: Pi coding agent frontend for CardputerZero
 A framebuffer client with project selection, session management and offline pinyin.
"""
    dist = output_dir
    dist.mkdir(parents=True, exist_ok=True)
    output = dist / f"{PACKAGE}_{VERSION}_arm64.deb"
    with TemporaryDirectory(dir=dist) as tmp:
        staging = Path(tmp)
        (staging / "debian-binary").write_bytes(b"2.0\n")
        write_tar(staging / "control.tar.gz", {"control": (control.encode(), 0o644)})
        modules = {
            f"usr/lib/{PACKAGE}/card_pi/{source.name}": (source.read_bytes(), 0o644)
            for source in (SOURCE.parent / "card_pi").iterdir()
            if source.suffix in {".py", ".mjs"}
        }
        write_tar(staging / "data.tar.gz", {
            **modules,
            f"usr/share/doc/{PACKAGE}/copyright": ((SOURCE.parent / "LICENSE").read_bytes(), 0o644),
            f"usr/share/doc/{PACKAGE}/ICON_NOTICE": ((SOURCE / "ICON_NOTICE").read_bytes(), 0o644),
            f"usr/bin/{PACKAGE}": ((SOURCE / PACKAGE).read_bytes(), 0o755),
            f"usr/share/APPLaunch/applications/{PACKAGE}.desktop": (
                (SOURCE / f"{PACKAGE}.desktop").read_bytes(), 0o644
            ),
            f"usr/share/APPLaunch/share/images/{PACKAGE}.png": ((SOURCE / f"{PACKAGE}.png").read_bytes(), 0o644),
        })
        write_deb(
            staging / output.name,
            [staging / name for name in ("debian-binary", "control.tar.gz", "data.tar.gz")],
        )
        (staging / output.name).replace(output)
    print(output)


if __name__ == "__main__":
    main()
