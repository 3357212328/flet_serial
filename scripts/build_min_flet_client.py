"""Build a minimal Flet Windows desktop client without unused Flutter extensions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT_DIR / "build" / "min_flet_client" / "flet"
DEFAULT_WORK_DIR = ROOT_DIR / "build" / "min_flet_work"
ASSETS_ICO = ROOT_DIR / "assets" / "icon.ico"


def detect_flet_version() -> str:
    import flet.version

    return str(flet.version.flet_version)


def find_flutter_bin() -> str:
    candidates = [
        shutil.which("flutter"),
        shutil.which("flutter.bat"),
        os.environ.get("FLUTTER_ROOT") and str(Path(os.environ["FLUTTER_ROOT"]) / "bin" / "flutter.bat"),
        r"D:\flet_build_tools\flutter\bin\flutter.bat",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Flutter SDK not found. Install Flutter 3.47.x or set FLUTTER_ROOT / PATH."
    )


def ensure_flet_source(flet_src: Path | None, version: str, work_dir: Path) -> Path:
    if flet_src is not None:
        if (flet_src / "client").is_dir() and (flet_src / "packages" / "flet").is_dir():
            return flet_src
        raise FileNotFoundError(f"Invalid --flet-src directory: {flet_src}")

    local_cached = Path(r"D:\flet_build_tools\flet-src")
    if (local_cached / "client").is_dir() and (local_cached / "packages" / "flet").is_dir():
        return local_cached

    cloned = work_dir / "flet-src"
    if (cloned / "client").is_dir() and (cloned / "packages" / "flet").is_dir():
        return cloned

    cloned.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            f"v{version}",
            "--filter=blob:none",
            "--sparse",
            "https://github.com/flet-dev/flet.git",
            str(cloned),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(cloned), "sparse-checkout", "set", "--no-cone", "client", "packages/flet"],
        check=True,
    )
    return cloned


def prepare_minimal_project(flet_src: Path, work_dir: Path, version: str) -> Path:
    proj = work_dir / "min_flet"
    if proj.exists():
        shutil.rmtree(proj)

    (proj / "packages").mkdir(parents=True, exist_ok=True)
    shutil.copytree(flet_src / "packages" / "flet", proj / "packages" / "flet")
    shutil.copytree(flet_src / "client", proj / "client")
    shutil.rmtree(proj / "client" / "integration_test", ignore_errors=True)

    client_dir = proj / "client"
    (client_dir / "pubspec.yaml").write_text(
        f"""name: flet_client
description: Minimal Flet desktop client for OmniPort Studio.
publish_to: "none"
version: {version}

environment:
  sdk: '>=3.0.0 <4.0.0'

dependencies:
  flutter:
    sdk: flutter
  flutter_web_plugins:
    sdk: flutter
  flet:
    path: ../packages/flet

dev_dependencies:
  flutter_test:
    sdk: flutter

flutter:
  uses-material-design: true
""",
        encoding="utf-8",
    )

    main_dart = (client_dir / "lib" / "main.dart").read_text(encoding="utf-8")
    kept_imports = "\n".join(
        [
            "import 'package:flet/flet.dart';",
            "import 'package:flutter/foundation.dart';",
            "import 'package:flutter/widgets.dart';",
            "import 'package:flutter_web_plugins/url_strategy.dart';",
        ]
    )
    body = main_dart[main_dart.index("const bool isProduction") :]
    ext_start = body.index("List<FletExtension> extensions = [")
    ext_end = body.index("];", ext_start) + 2
    body = body[:ext_start] + "List<FletExtension> extensions = [];" + body[ext_end:]
    (client_dir / "lib" / "main.dart").write_text(
        f"{kept_imports}\n\n{body}",
        encoding="utf-8",
    )

    cmake_path = client_dir / "windows" / "CMakeLists.txt"
    cmake_text = cmake_path.read_text(encoding="utf-8")
    target_opt = 'target_compile_options(${TARGET} PRIVATE /W4 /WX /wd"4100")'
    if target_opt in cmake_text and "/utf-8" not in cmake_text:
        cmake_text = cmake_text.replace(
            target_opt,
            'target_compile_options(${TARGET} PRIVATE /W4 /WX /wd"4100" /utf-8)',
            1,
        )
        cmake_path.write_text(cmake_text, encoding="utf-8")

    if ASSETS_ICO.is_file():
        runner_ico = client_dir / "windows" / "runner" / "resources" / "app_icon.ico"
        if runner_ico.parent.is_dir():
            shutil.copy2(ASSETS_ICO, runner_ico)

    return client_dir


def build_minimal_client(
    output_dir: Path,
    work_dir: Path,
    flet_src: Path | None = None,
    use_cn_mirrors: bool = False,
) -> Path:
    version = detect_flet_version()
    flutter_bin = find_flutter_bin()
    src_root = ensure_flet_source(flet_src, version, work_dir)
    client_dir = prepare_minimal_project(src_root, work_dir, version)

    symbols_dir = work_dir / "symbols"
    if symbols_dir.exists():
        shutil.rmtree(symbols_dir)
    symbols_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if use_cn_mirrors:
        env.setdefault("PUB_HOSTED_URL", "https://pub.flutter-io.cn")
        env.setdefault("FLUTTER_STORAGE_BASE_URL", "https://storage.flutter-io.cn")

    subprocess.run([flutter_bin, "pub", "get"], cwd=client_dir, env=env, check=True)
    subprocess.run(
        [
            flutter_bin,
            "build",
            "windows",
            "--release",
            f"--build-name={version}",
            "--build-number=1",
            "--obfuscate",
            f"--split-debug-info={symbols_dir}",
        ],
        cwd=client_dir,
        env=env,
        check=True,
    )

    release_dir = client_dir / "build" / "windows" / "x64" / "runner" / "Release"
    if not (release_dir / "flet.exe").is_file():
        raise RuntimeError(f"Expected Flutter release output missing at {release_dir}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(release_dir, output_dir)

    sys32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"
    for dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        src_dll = sys32 / dll
        if src_dll.is_file():
            shutil.copy2(src_dll, output_dir / dll)

    total_bytes = sum(p.stat().st_size for p in output_dir.rglob("*") if p.is_file())
    file_count = sum(1 for p in output_dir.rglob("*") if p.is_file())
    print(f"Built minimal Flet client: {output_dir} ({file_count} files, {total_bytes / 1024 / 1024:.2f} MB raw)")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build minimal Flet Windows client.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--flet-src", type=Path, default=None)
    parser.add_argument("--cn-mirrors", action="store_true")
    args = parser.parse_args()
    build_minimal_client(
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        flet_src=args.flet_src,
        use_cn_mirrors=args.cn_mirrors,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
