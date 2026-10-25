# Champollion Workbench
<img width="790" height="1098" alt="image" src="https://github.com/user-attachments/assets/daaf3174-1de7-4bbe-8e62-c5b875f200cd" />
<img width="790" height="1098" alt="image" src="https://github.com/user-attachments/assets/e55eb061-e38f-410e-8b51-020da8f61f25" />


A native GTK3 (X11/Xorg) desktop front-end for the Papyrus scripting
toolchain used to mod Skyrim / Fallout 4 / Fallout 76 / Starfield:

* **Decompile** tab &mdash; drives **Champollion** (`.pex` &rarr; `.psc`)
* **Recompile** tab &mdash; drives **Caprica** (`.psc` &rarr; `.pex`)
* **Build & Setup** tab &mdash; locate/test your binaries, and build
  Champollion from source with one click

This app is a *launcher*, not the tools themselves — it shells out to the
real `Champollion` / `Caprica` executables with the flags you choose, and
streams their output live into a log pane.

## Important finding about the archive you uploaded

Your `Champollion.zip` bundles two separate projects:

| Tool | Direction | Portability |
|---|---|---|
| **Champollion** | `.pex` &rarr; `.psc` (decompile) | Cross-platform. Builds cleanly on Linux with CMake + Boost + fmt. |
| **Caprica** (vendored copy) | `.psc` &rarr; `.pex` (recompile) | **Windows-only as shipped** &mdash; `Caprica/main.cpp` does an unconditional `#include <Windows.h>` and isn't guarded by `#ifdef _WIN32`, and it also isn't wired into the top-level `CMakeLists.txt` (`add_subdirectory(Caprica)` is never called). |

So the honest situation for "recompile on Linux/X11": you have three
options, and the GUI supports all of them —

1. **Build Caprica on Windows** (or cross-compile with MinGW) and run the
   resulting `Caprica.exe` on Linux through **Wine**. The Setup tab has a
   "Run Caprica through Wine" checkbox for exactly this — point the
   Caprica field at the `.exe` and it will invoke `wine Caprica.exe ...`
   for you.
2. **Port `Caprica/main.cpp`** yourself (the `Windows.h` usage looks limited
   to things like console signal handling — replacing it with POSIX
   equivalents and adding the missing `add_subdirectory(Caprica)` +
   `find_package` wiring in `CMakeLists.txt` is a real but bounded task).
   Happy to help with that porting work directly if you want it.
3. **Use a Linux-friendly Caprica fork**, if one exists that meets your
   needs, and just point the Setup tab at that binary — the GUI doesn't
   care where the executable came from.

I did not fabricate a build for Caprica or silently skip this — better to
tell you up front than hand you a GUI button that fails.

I also could not actually compile Champollion in this sandbox to hand you
a working binary directly: the sandbox has no network access, and Boost/
fmt/cmake aren't preinstalled here. `scripts/build_champollion.sh` does
exactly what would be needed — run it on your own machine (or click
"Build Champollion" in the Setup tab) once the dependencies below are
installed.

## Requirements

**To run the GUI:**
```
# Debian/Ubuntu
sudo apt install python3-gi gir1.2-gtk-3.0
# Fedora
sudo dnf install python3-gobject gtk3
# Arch
sudo pacman -S python-gobject gtk3
```

**To build Champollion from source:**
```
# Debian/Ubuntu
sudo apt install cmake build-essential libboost-program-options-dev libfmt-dev
# Fedora
sudo dnf install cmake gcc-c++ boost-devel fmt-devel
# Arch
sudo pacman -S cmake base-devel boost fmt
```

## Running

```
python3 champollion_gui.py
```

On first run, go to **Build & Setup**:

1. Point "Champollion executable" at your built binary (or use "Build
   Champollion from source" below it — pick your extracted source folder
   and a build folder, then click **Build Champollion**).
2. Point "Caprica executable" at whatever Caprica binary you have
   available (native Linux port, or a Windows `.exe` + tick "Run Caprica
   through Wine").
3. Click **Save settings** — these are remembered at
   `~/.config/champollion-gui/config.json` for next time.

Then use the **Decompile** and **Recompile** tabs: add input `.pex`/`.psc`
files or whole folders, pick an output folder, tick the options you want,
and hit **Run**. Output streams live into the log pane below, and you can
**Cancel** a run in progress.

An **Extra arguments** field is included on both tabs for any CLI flag not
exposed as a checkbox — passed through verbatim.

## Optional: desktop launcher

`champollion-workbench.desktop` is included for X11 desktop environments
(GNOME, KDE, XFCE, etc.). Edit the `Exec=` line to the real path where you
put `champollion_gui.py`, then copy it to `~/.local/share/applications/`.

## Files

```
champollion-gui/
├── champollion_gui.py            # the app
├── champollion-workbench.desktop # optional launcher
├── scripts/
│   └── build_champollion.sh      # cmake build helper
└── README.md
```
