"""Build a self-contained OmniPort Studio executable (< 50 MB) using Nuitka."""

from __future__ import annotations

import argparse
import ast
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tomllib
import zipfile

ROOT_DIR = Path(__file__).resolve().parent
BUILD_DIR = ROOT_DIR / "build"
DIST_DIR = ROOT_DIR / "dist"
ASSETS_DIR = ROOT_DIR / "assets"
ICON_ICO = ASSETS_DIR / "icon.ico"

MAX_RELEASE_BYTES = 50 * 1024 * 1024  # 50 MB hard ceiling
MIN_CLIENT_DEFAULT_DIR = BUILD_DIR / "min_flet_client" / "flet"

EXCLUDED_MODULES = (
    "flet_web",
    "flet_cli",
    "flet.testing",
    "flet.pytest_plugin",
    "flet_js",
    "flet.auth",
    "flet.fastapi",
    "flet.security",
    "ssl",
    "_ssl",
    "_hashlib",
    "bleak.backends.bluezdbus",
    "bleak.backends.corebluetooth",
    "bleak.backends.p4android",
    "dbus_fast",
    "objc",
    "Foundation",
    "CoreBluetooth",
    "CoreFoundation",
    "libdispatch",
    "jnius",
    "android",
    "fastapi",
    "uvicorn",
    "starlette",
    "pydantic",
    "pydantic_core",
    "PIL",
    "resvg_py",
    "httpx",
    "httpcore",
    "requests",
    "urllib3",
    "watchfiles",
    "watchdog",
    "cookiecutter",
    "jinja2",
    "qrcode",
    "oauthlib",
    "opentelemetry",
    "pygments",
    "markdown_it",
    "yaml",
    "arrow",
    "click",
    "chardet",
    "charset_normalizer",
    "tzdata",
    "websockets",
    "numpy",
    "skimage",
    "pytest",
    "griffe",
    "setuptools",
)


def collect_flet_runtime_includes() -> tuple[list[str], list[str]]:
    """Resolve exact lazy Flet control modules referenced by project sources."""
    import flet

    used_ft: set[str] = set()
    py_files = [ROOT_DIR / "main.py", *(ROOT_DIR / "serial_assistant").glob("*.py")]
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ft"
            ):
                used_ft.add(node.attr)

    lazy_map = getattr(flet, "_LAZY", {})
    modules: set[str] = {
        "flet.app",
        "flet.canvas",
        "flet.controls.core.canvas",
        "flet.data_channel",
    }
    for attr in used_ft:
        target = lazy_map.get(attr)
        if isinstance(target, tuple):
            modules.add(str(target[0]))
        elif isinstance(target, str):
            modules.add(target)

    # cupertino_colors is imported by flet.controls.types; exclude unused cupertino controls.
    cupertino_dir = Path(flet.__file__).resolve().parent / "controls" / "cupertino"
    unused_cupertino = [
        f"flet.controls.cupertino.{p.stem}"
        for p in sorted(cupertino_dir.glob("*.py"))
        if p.stem not in ("__init__", "cupertino_colors")
    ]
    return sorted(modules), unused_cupertino


def read_project_version() -> str:
    data = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def to_four_part_version(version: str) -> str:
    parts = [p for p in version.split(".") if p.isdigit()]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def patch_exe_icon(exe_path: Path, ico_path: Path, group_id: int = 101) -> None:
    """Replace the RT_GROUP_ICON / RT_ICON resources in a Windows executable."""
    raw = ico_path.read_bytes()
    reserved, img_type, count = struct.unpack_from("<HHH", raw, 0)
    if reserved != 0 or img_type != 1 or count == 0:
        raise ValueError(f"Invalid ICO file: {ico_path}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.WORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    def make_int_resource(res_id: int) -> wintypes.LPCWSTR:
        return ctypes.cast(ctypes.c_void_p(res_id), wintypes.LPCWSTR)

    RT_ICON = make_int_resource(3)
    RT_GROUP_ICON = make_int_resource(14)
    LANG_EN_US = 1033

    grp_entries = bytearray(struct.pack("<HHH", 0, 1, count))
    images: list[tuple[int, bytes]] = []
    for idx in range(count):
        w, h, color_cnt, res, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + idx * 16
        )
        icon_id = idx + 1
        grp_entries.extend(
            struct.pack(
                "<BBBBHHIH",
                w,
                h,
                color_cnt,
                res,
                planes,
                bpp,
                size,
                icon_id,
            )
        )
        images.append((icon_id, raw[offset : offset + size]))

    handle = kernel32.BeginUpdateResourceW(str(exe_path), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    discard = True
    try:
        grp_buf = (ctypes.c_ubyte * len(grp_entries)).from_buffer_copy(grp_entries)
        if not kernel32.UpdateResourceW(
            handle, RT_GROUP_ICON, make_int_resource(group_id), LANG_EN_US, grp_buf, len(grp_entries)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        for icon_id, img_bytes in images:
            img_buf = (ctypes.c_ubyte * len(img_bytes)).from_buffer_copy(img_bytes)
            if not kernel32.UpdateResourceW(
                handle, RT_ICON, make_int_resource(icon_id), LANG_EN_US, img_buf, len(img_bytes)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        discard = False
    finally:
        if not kernel32.EndUpdateResourceW(handle, discard) and not discard:
            raise ctypes.WinError(ctypes.get_last_error())


def resolve_flet_client_dir(
    flet_client_dir: Path | None = None,
    allow_full_client: bool = False,
) -> Path:
    """Locate or build the minimal stripped Flet desktop client directory."""
    candidates: list[Path] = []
    if flet_client_dir is not None:
        candidates.append(flet_client_dir)
    env_dir = os.environ.get("OMNIPORT_FLET_CLIENT_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(MIN_CLIENT_DEFAULT_DIR)

    for candidate in candidates:
        if (candidate / "flet.exe").is_file() and (candidate / "data" / "app.so").is_file():
            print(f"Using minimal Flet client: {candidate}")
            return candidate

    if not allow_full_client:
        min_builder = ROOT_DIR / "scripts" / "build_min_flet_client.py"
        if min_builder.is_file():
            try:
                subprocess.run(
                    [sys.executable, str(min_builder), "--output-dir", str(MIN_CLIENT_DEFAULT_DIR)],
                    cwd=str(ROOT_DIR),
                    check=True,
                )
                if (MIN_CLIENT_DEFAULT_DIR / "flet.exe").is_file():
                    return MIN_CLIENT_DEFAULT_DIR
            except Exception as exc:
                raise RuntimeError(
                    "Minimal Flet client not found and auto-build failed. "
                    "Build it via scripts/build_min_flet_client.py or pass --allow-full-client."
                ) from exc

    import flet_desktop

    cache_dir = Path(flet_desktop.ensure_client_cached())
    source_flet_dir = cache_dir / "flet"
    if not source_flet_dir.is_dir():
        raise FileNotFoundError(f"Flet desktop client not found at {source_flet_dir}")
    print(f"Warning: falling back to full Flet client: {source_flet_dir}")
    return source_flet_dir


def stage_flet_desktop_archive(
    stage_dir: Path,
    flet_client_dir: Path | None = None,
    allow_full_client: bool = False,
) -> Path:
    """Create a deterministic LZMA-compressed flet-windows.zip + sha256 sidecar."""
    source_flet_dir = resolve_flet_client_dir(
        flet_client_dir=flet_client_dir,
        allow_full_client=allow_full_client,
    )

    temp_flet_dir = BUILD_DIR / "flet_stage" / "flet"
    if temp_flet_dir.parent.exists():
        shutil.rmtree(temp_flet_dir.parent)
    shutil.copytree(source_flet_dir, temp_flet_dir)

    flet_exe = temp_flet_dir / "flet.exe"
    if ICON_ICO.is_file() and flet_exe.is_file():
        patch_exe_icon(flet_exe, ICON_ICO)
        data_icon = temp_flet_dir / "data" / "app_icon.ico"
        data_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ICON_ICO, data_icon)

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    archive_path = stage_dir / "flet-windows.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_LZMA) as zf:
        for root, dirs, files in os.walk(temp_flet_dir):
            dirs.sort()
            for fname in sorted(files):
                full_path = Path(root) / fname
                arcname = full_path.relative_to(temp_flet_dir.parent).as_posix()
                zi = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_LZMA
                zi.external_attr = (os.stat(full_path).st_mode & 0xFFFF) << 16
                with full_path.open("rb") as src, zf.open(zi, "w") as dst:
                    shutil.copyfileobj(src, dst)

    sha256 = hashlib.sha256()
    with archive_path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            sha256.update(chunk)
    size = archive_path.stat().st_size
    (stage_dir / "flet-windows.zip.sha256").write_text(
        f"{sha256.hexdigest()} {size}", encoding="utf-8"
    )
    shutil.rmtree(temp_flet_dir.parent, ignore_errors=True)
    print(f"Staged flet-windows.zip: {size / (1024 * 1024):.2f} MB")
    return stage_dir


def build(
    output_name: str = "OmniPortStudio.exe",
    debug_standalone: bool = False,
    flet_client_dir: Path | None = None,
    allow_full_client: bool = False,
) -> Path:
    version = read_project_version()
    four_part_version = to_four_part_version(version)

    stage_dir = stage_flet_desktop_archive(
        BUILD_DIR / "flet_desktop_app",
        flet_client_dir=flet_client_dir,
        allow_full_client=allow_full_client,
    )
    include_modules, unused_cupertino = collect_flet_runtime_includes()
    target_dist = (BUILD_DIR / "debug_standalone") if debug_standalone else DIST_DIR
    target_dist.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--python-flag=no_docstrings",
    ]
    if not debug_standalone:
        cmd.extend(
            [
                "--onefile",
                "--windows-console-mode=disable",
                "--remove-output",
            ]
        )
    cmd.extend(
        [
            f"--windows-icon-from-ico={ICON_ICO}",
            "--company-name=OmniPort Studio",
            "--product-name=OmniPort Studio",
            f"--file-version={four_part_version}",
            f"--product-version={version}",
            "--file-description=OmniPort Studio - Serial / BLE / Network Debugger",
            f"--output-dir={target_dist}",
            f"--output-filename={output_name}",
            "--assume-yes-for-downloads",
            f"--include-data-dir={ASSETS_DIR}=assets",
            f"--include-data-dir={stage_dir}=flet_desktop/app",
            "--include-package-data=flet:controls/material/icons.json",
            "--include-package-data=winrt:*.pywinrt",
            "--include-package=flet.messaging",
            "--include-package=flet.pubsub",
            "--include-package=flet.utils",
            "--include-package=flet_desktop",
            "--include-package=bleak.backends.winrt",
            "--include-package=winrt",
        ]
    )
    for mod in include_modules:
        cmd.append(f"--include-module={mod}")
    for mod in (*EXCLUDED_MODULES, *unused_cupertino):
        cmd.append(f"--nofollow-import-to={mod}")
    cmd.append(str(ROOT_DIR / "main.py"))

    print("Running Nuitka build:")
    print(" ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT_DIR), check=True)

    out_exe = (target_dist / "main.dist" / output_name) if debug_standalone else (target_dist / output_name)
    if not out_exe.is_file():
        raise FileNotFoundError(f"Expected output executable not found: {out_exe}")

    size_bytes = out_exe.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    print(f"Built {out_exe} ({size_mb:.2f} MB / {size_bytes:,} bytes)")
    if not debug_standalone and size_bytes > MAX_RELEASE_BYTES:
        raise RuntimeError(
            f"Release executable exceeds 50 MB limit: {size_mb:.2f} MB ({size_bytes} bytes)"
        )
    return out_exe


def main() -> None:
    parser = argparse.ArgumentParser(description="Build OmniPort Studio with Nuitka.")
    parser.add_argument(
        "--output-name",
        default="OmniPortStudio.exe",
        help="Output executable filename inside dist/",
    )
    parser.add_argument(
        "--debug-standalone",
        action="store_true",
        help="Build an uncompressed standalone folder with console enabled for smoke testing.",
    )
    parser.add_argument(
        "--flet-client-dir",
        type=Path,
        default=None,
        help="Path to a prebuilt minimal Flet Windows client directory containing flet.exe.",
    )
    parser.add_argument(
        "--allow-full-client",
        action="store_true",
        help="Allow falling back to the full cached flet-desktop client if minimal build is unavailable.",
    )
    args = parser.parse_args()
    build(
        output_name=args.output_name,
        debug_standalone=args.debug_standalone,
        flet_client_dir=args.flet_client_dir,
        allow_full_client=args.allow_full_client,
    )


if __name__ == "__main__":
    main()
