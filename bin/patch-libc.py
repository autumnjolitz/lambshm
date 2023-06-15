#!/usr/bin/env python
"""
Binary patch libc to use an alternate root for shared memory (``shm_open``) calls.
"""
import os
import sys
from pathlib import Path
from typing import Optional, Union, Set, Tuple, List, Iterable

from pwnlib.elf.elf import ELF


def main(
    search_paths: Optional[Tuple[str, ...]] = None,
    write_prefix: Optional[Union[str, Path]] = None,
    shm_folder: Optional[str] = None,
) -> List[ELF]:
    if write_prefix is not None:
        if not isinstance(write_prefix, Path):
            write_prefix = Path(write_prefix)
        try:
            write_prefix = write_prefix.resolve(True)
        except FileNotFoundError:
            write_prefix.mkdir(parents=True, exist_ok=True)
        if not write_prefix.is_dir():
            raise NotADirectoryError(f"{write_prefix} is not a directory")
    search_paths = tuple(Path(x) if not isinstance(x, Path) else x for x in search_paths)
    search_paths = (path for path in search_paths if path.is_dir())
    library: ELF
    libraries = []
    for library in patch_libraries(search_paths, shm_folder):
        if write_prefix is None:
            filename = None
        else:
            if write_prefix == "/":
                library.save()
                filename = library.name
            else:
                source_path = Path(library.path).resolve(True)
                source_directory = str(source_path.parent)[1:]
                os.makedirs(write_prefix / source_directory, exist_ok=True)
                filename = str(write_prefix / str(source_path)[1:])
                library.save(filename)
        print("Patching", library, "->", filename, file=sys.stderr)
        libraries.append(library)
    return libraries


def patch_library(path: Path, shm_folder: Optional[str] = None) -> Optional[ELF]:
    if not path.name.startswith(("ld-linux", "libpthread")):
        return
    if shm_folder is None:
        shm_folder = "/tmp/shm/"
    library = ELF(path)
    has_default_dir = "defaultdir" in library.symbols
    has_where_is_shmfs = "where_is_shmfs" in library.functions

    if any((has_default_dir, has_where_is_shmfs)):
        print(f"opening {path}", file=sys.stderr)
        if has_default_dir:
            current_default = (
                library.read(library.symbols["defaultdir"], 16).strip(b"\x00").decode()
            )
            if len(shm_folder.encode()) != len(current_default.encode()):
                raise ValueError(
                    f"length of shm_folder ({shm_folder.encode()!r}, {len(shm_folder.encode())}) must be the same as the prior value ({current_default.encode()!r}, {len(current_default.encode())})"
                )
            print(f"setting defaultdir -> {current_default} -> {shm_folder}", file=sys.stderr)
            library.write(library.symbols["defaultdir"], shm_folder.encode())
        if has_where_is_shmfs:
            whereis_shmfs = library.functions["where_is_shmfs"]
            print(
                "Function is\n",
                library.disasm(whereis_shmfs.address, whereis_shmfs.size),
                file=sys.stderr,
            )
            library.asm(library.functions["where_is_shmfs"].address + 24 + 22 + 0, "jmp 0xef91;")
            print(
                "Patched to\n",
                library.disasm(whereis_shmfs.address, whereis_shmfs.size),
                file=sys.stderr,
            )

        return library


def traverse_tree(
    origin: Path,
    *,
    memo: Optional[Set[str]] = None,
    depth_limit: Optional[int] = None,
    depth: int = 0,
) -> Iterable[Path]:
    flat_origin = f"{origin!s}"
    if memo is None:
        memo = set()
    if depth_limit is None:
        depth_limit = float("inf")

    dirs = []

    for file in origin.iterdir():
        while file.is_symlink():
            file = Path(os.readlink(file))
        filename = f"{file!s}"
        if not filename.startswith(flat_origin):
            # outside of the origin
            continue
        if filename in memo:
            continue
        if file.is_dir() and depth < depth_limit:
            dirs.append(file)
            continue
        if ".so" not in file.name:
            continue
        with open(file, "rb") as fh:
            header = fh.read(16)
            if not header.startswith(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"):
                continue
        yield file
    for file in dirs:
        for entry in traverse_tree(file, memo=memo, depth=depth + 1, depth_limit=depth_limit):
            yield entry


def patch_libraries(
    search_paths: Optional[Tuple[Path, ...]] = None,
    shm_folder: Optional[str] = None,
) -> Iterable[ELF]:
    if search_paths is None:
        search_paths = Path("/").glob("lib*")

    file: Path
    seen: Set[str] = set()
    for search in search_paths:
        if search.is_dir():
            for file in traverse_tree(search, memo=seen, depth_limit=1):
                value = patch_library(file, shm_folder)
                if value is not None:
                    yield value


if __name__ == "__main__":
    import argparse
    import glob

    search_paths: Tuple[str, ...] = glob.glob("/lib*")

    parser = argparse.ArgumentParser(
        description="Patch out references to /dev/shm in libc and replace with /tmp/shm"
    )
    parser.set_defaults(prefix=None, search_paths=[])
    parser.add_argument("--shm-folder", default=None, help="defaults to /tmp/shm/")
    parser.add_argument(
        "--search-in", action="append", dest="search_paths", help=f"Defaults to {search_paths}"
    )
    excl = parser.add_mutually_exclusive_group()
    excl.add_argument("--overwrite", action="store_const", const="/", dest="prefix")
    excl.add_argument("--copy-to", type=str, dest="prefix")
    args = parser.parse_args()
    libs = main(args.search_paths or search_paths, args.prefix, args.shm_folder)
    if not libs:
        raise FileNotFoundError("didn't patch anything?!")
