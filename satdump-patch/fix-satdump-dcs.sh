#!/usr/bin/env bash
# Fix SatDump's GOES "Parse DCS" hang: patch the source, rebuild the one plugin, install it.
# See README.md beside this script for what it fixes and what it changes on your machine.
#
#   ./fix-satdump-dcs.sh [options] /path/to/SatDump [build-folder]
#
# The script itself is BSD-3-Clause like the rest of GOESview. The patches beside it change
# SatDump and are offered under SatDump's licence (GPL-3.0).

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NEW_ADDRESS="https://dadds-dcs-api.data.nesdis.noaa.gov/pdts/PDTS_COMPRESSED.txt"
DCS=plugins/goes_support/goes/hrit/dcs
SOURCES=(dcs_decoder.cpp dcs_settings.cpp)   # what the patches change (plus the new dcs_tables.h)
BACKUP_SUFFIX=.before-dcs-fix

mode=fix full=0 force=0 settings=1 stage_root=${DESTDIR:-}
src="" build=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] /path/to/SatDump [build-folder]

Patches SatDump's source, rebuilds only the GOES plugin and installs it, keeping a copy
of the plugin it replaces. The build folder defaults to <SatDump>/build.

  --check          say what would be done and change nothing
  --patch-only     patch the source and stop (build and install SatDump yourself)
  --full-build     let the build system rebuild whatever it thinks is out of date,
                   instead of only the two changed files (slower, sometimes much)
  --no-settings    do not add NOAA's new address to SatDump's settings.json
  --settings-only  only add NOAA's new address to settings.json (SatDump must be closed)
  --undo           put back the plugin that was replaced, and take the patch out again
  --force          go on even if the installed SatDump is not this source's version
  -h, --help       this text

Set DESTDIR=/some/folder to install under that folder instead of the real system (to try it).
EOF
}

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --check) mode=check ;;
        --patch-only) mode=patch ;;
        --settings-only) mode=settings ;;
        --undo) mode=undo ;;
        --full-build) full=1 ;;
        --no-settings) settings=0 ;;
        --force) force=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; die "unknown option $1" ;;
        *) if [ -z "$src" ]; then src=$1; elif [ -z "$build" ]; then build=$1; else die "too many folders given"; fi ;;
    esac
    shift
done

if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ] && [ -z "$stage_root" ]; then
    die "run this as yourself, not with sudo: it asks for sudo only for the one step that needs it, and it has to find your own SatDump settings"
fi

satdump_running() { pgrep -x satdump-ui >/dev/null 2>&1 || pgrep -x satdump >/dev/null 2>&1; }

# ------------------------------------------------------------------ settings.json

settings_file() { printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/satdump/settings.json"; }

# Prints one of: none (no file, or no address list in it: SatDump's new default applies),
# present, missing, unreadable.
settings_state() {
    local file; file=$(settings_file)
    [ -f "$file" ] || { say none; return; }
    command -v python3 >/dev/null 2>&1 || { say unreadable; return; }
    python3 - "$file" "$NEW_ADDRESS" <<'EOF' 2>/dev/null || say unreadable
import json, sys
try:
    urls = json.load(open(sys.argv[1], encoding="utf-8"))["plugin_settings"]["goes_support"]["pdt_urls"]
except (KeyError, TypeError):
    print("none")
else:
    print("present" if sys.argv[2] in urls else "missing")
EOF
}

add_address() {
    local file state; file=$(settings_file); state=$(settings_state)
    case "$state" in
        none) say "settings.json has no PDT address list of its own: SatDump's new default (which has the new address) applies." ;;
        present) say "settings.json already has NOAA's new address." ;;
        unreadable)
            say "Could not read $file (python3 is needed for this step)."
            say "Add this as the first entry of plugin_settings > goes_support > pdt_urls, with SatDump closed:"
            say "    $NEW_ADDRESS" ;;
        missing)
            if [ "$mode" = check ]; then
                say "Would add NOAA's new address to the front of pdt_urls in $file (keeping a copy as settings.json$BACKUP_SUFFIX)."
            elif satdump_running; then
                say "NOT added to settings.json, because SatDump is running and rewrites that file when it closes."
                say "Quit SatDump, then run:  $0 --settings-only"
                say "(Or add it in SatDump: Settings > GOES DCS > PDT URLs.)"
            else
                [ -e "$file$BACKUP_SUFFIX" ] || cp -p "$file" "$file$BACKUP_SUFFIX"
                python3 - "$file" "$NEW_ADDRESS" <<'EOF'
import json, os, sys
path, address = sys.argv[1:3]
with open(path, encoding="utf-8") as f:
    settings = json.load(f)
settings["plugin_settings"]["goes_support"]["pdt_urls"].insert(0, address)
with open(path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=4)
os.replace(path + ".tmp", path)
EOF
                say "Added NOAA's new address to the front of pdt_urls in $file"
                say "(the file as it was: settings.json$BACKUP_SUFFIX)."
            fi ;;
    esac
}

if [ "$mode" = settings ]; then
    step "NOAA's new platform-table address"
    satdump_running && die "SatDump is running. Quit it first: it rewrites settings.json when it closes."
    add_address
    exit 0
fi

# ------------------------------------------------------------------ the source

[ -n "$src" ] || { usage >&2; die "give the folder that holds SatDump's source"; }
[ -d "$src" ] || die "$src is not a folder"
src=$(cd "$src" && pwd -P)
[ -f "$src/$DCS/dcs_decoder.cpp" ] || die "$src does not look like SatDump's source (no $DCS/dcs_decoder.cpp)"
build=${build:-$src/build}
case "$build" in /*) ;; *) build=$PWD/$build ;; esac

applies() { (cd "$src" && git apply --check "$@" >/dev/null 2>&1); }

# Which patch fits, and is it in already? Sets state (patched, unpatched, other, buggy,
# unknown) and, for the first two, patch_file.
patch_file="" state=unknown
for p in "$HERE"/satdump-*-dcs.patch; do
    if applies -R "$p"; then patch_file=$p state=patched; break; fi
done
if [ "$state" = unknown ]; then
    for p in "$HERE"/satdump-*-dcs.patch; do
        if applies "$p"; then patch_file=$p state=unpatched; break; fi
    done
fi
if [ "$state" = unknown ]; then
    if [ -f "$src/$DCS/dcs_tables.h" ]; then
        state=other   # the fix is there, though not exactly as these patch files have it
    elif grep -q 'dcp_mtx\.lock()' "$src/$DCS/dcs_decoder.cpp" \
            && grep -q 'dcp_list\[std::stoul(pdt_line' "$src/$DCS/dcs_decoder.cpp"; then
        state=buggy   # a lock taken by hand, and the line that throws while it is held
    fi
fi

# Where this build installs the GOES plugin, or nothing if that cannot be told. The list
# CMake wrote at the last install is the best witness; failing that, the plugin's own
# install script names the folder.
cache() { sed -n "s/^$1:[A-Z]*=//p" "$build/CMakeCache.txt" 2>/dev/null | head -n 1; }
installed_plugin() {
    local found="" dir prefix
    if [ -f "$build/install_manifest.txt" ]; then
        found=$(grep -m1 '/satdump/plugins/libgoes_support\.so$' "$build/install_manifest.txt" || true)
    fi
    if [ -z "$found" ] && [ -f "$build/plugins/goes_support/cmake_install.cmake" ]; then
        dir=$(sed -n 's/.*file(INSTALL DESTINATION "\([^"]*satdump\/plugins\)".*/\1/p' "$build/plugins/goes_support/cmake_install.cmake" | head -n 1)
        prefix=$(cache CMAKE_INSTALL_PREFIX)
        [ -z "$dir" ] || found="${dir/\$\{CMAKE_INSTALL_PREFIX\}/$prefix}/libgoes_support.so"
    fi
    printf '%s\n' "$found"
}

step "SatDump source: $src"
command -v git >/dev/null 2>&1 || die "git is needed (to apply the patch)"
revision=$(git -C "$src" rev-parse HEAD 2>/dev/null || true)
[ -z "$revision" ] || say "At revision ${revision:0:8}."

if [ "$mode" = undo ]; then
    undone=0
    plugin=$(installed_plugin)
    if [ -n "$plugin" ] && [ -f "$stage_root$plugin$BACKUP_SUFFIX" ]; then
        target=$stage_root$plugin
        sudo=""; [ -w "$(dirname "$target")" ] || sudo=sudo
        $sudo cp -p "$target$BACKUP_SUFFIX" "$target.undo.$$"
        $sudo mv -f "$target.undo.$$" "$target"
        say "Put back $target from its copy. Restart SatDump to load it."
        undone=1
    else
        say "No copy of a replaced plugin was found${plugin:+ beside $stage_root$plugin}: nothing to put back."
    fi
    if [ "$state" = patched ]; then
        (cd "$src" && git apply -R "$patch_file")
        say "Took $(basename "$patch_file") out of the source again. The build folder still holds the patched plugin until SatDump is next built."
        undone=1
    else
        say "The source does not carry the patch."
    fi
    [ "$undone" = 1 ] || exit 1
    exit 0
fi

case "$state" in
    patched) say "Already patched ($(basename "$patch_file"))." ;;
    unpatched)
        if [ "$mode" = check ]; then
            say "Would apply $(basename "$patch_file")."
        else
            (cd "$src" && git apply "$patch_file")
            say "Applied $(basename "$patch_file")."
        fi ;;
    other) say "The source already carries this fix, though not exactly as the patch files here have it. Building it as it is." ;;
    buggy) die "this source has the bug, but neither patch fits it. The patches fit SatDump 1.2.3 builds from 3 January to 25 May 2025, and 2.0 (master) from 8 June 2025. Update the source to one of those, or make the changes by hand (see README.md)." ;;
    unknown) die "neither patch fits this source and it does not look like the code with the bug. It may already be fixed upstream, or be release 1.2.2 or older, which has a different DCS reader." ;;
esac

if [ "$mode" = patch ]; then
    say
    say "Source patched. Build and install SatDump the way you normally do; then see the settings note in README.md."
    exit 0
fi

# ------------------------------------------------------------------ the build

step "Build folder: $build"
if [ ! -f "$build/CMakeCache.txt" ]; then
    [ "$mode" = check ] || say "The source is patched, so a normal build of SatDump from it now includes the fix."
    die "there is no SatDump build in $build. Build and install SatDump from the patched source as its own instructions say, or give the build folder as a second argument."
fi
command -v cmake >/dev/null 2>&1 || die "cmake is needed"

generator=$(cache CMAKE_GENERATOR)
home=$(cache CMAKE_HOME_DIRECTORY)
[ -z "$home" ] || [ "$(cd "$home" 2>/dev/null && pwd -P)" = "$src" ] || die "that build folder was made from $home, not from $src"

installed=$(installed_plugin)
[ -n "$installed" ] || die "could not work out where this build installs the GOES plugin"
target=$stage_root$installed
say "This build installs the GOES plugin as $installed"
if [ -z "$stage_root" ] && [ ! -f "$target" ]; then
    die "there is no plugin at $target, so SatDump does not seem to be installed from this build. Install SatDump from it first (it now includes the fix), or see README.md."
fi

# The plugin must be built against the very SatDump that will load it. The core library in
# the build folder and the installed one carry the same build ID exactly when the installed
# SatDump was built here.
build_id() { readelf -n "$1" 2>/dev/null | sed -n 's/.*Build ID: *//p' | head -n 1; }
core=$(grep -m1 '/libsatdump_core\.[a-z]*$' "$build/install_manifest.txt" 2>/dev/null || true)
ours=$(build_id "$build/$(basename "${core:-libsatdump_core.so}")")
theirs=$([ -z "$core" ] || build_id "$core")
if [ -n "$ours" ] && [ -n "$theirs" ]; then
    if [ "$ours" = "$theirs" ]; then
        say "The installed SatDump was built in this build folder (same core library)."
    else
        say "The installed SatDump was NOT built in this build folder: $core is a different build."
        [ "$force" = 1 ] || die "a plugin built against a different SatDump may not load, or may crash it. Use the source and build folder your installed SatDump came from, or build and install all of SatDump from this source. (--force goes on regardless.)"
    fi
else
    say "Could not check that the installed SatDump was built in this build folder: make sure it was."
fi

built=$build/plugins/libgoes_support.so
target_dir=plugins/goes_support/CMakeFiles/goes_support.dir
quick=0
if [ "$full" = 0 ] && [ "$generator" = "Unix Makefiles" ] && [ -f "$build/$target_dir/build.make" ] \
        && [ -f "$build/$target_dir/link.txt" ] && [ -f "$built" ] && command -v make >/dev/null 2>&1; then
    quick=1
fi

if [ "$mode" = check ]; then
    if [ "$quick" = 1 ]; then
        say "Would compile the ${#SOURCES[@]} changed files and link the plugin again (a minute or two)."
    else
        say "Would run: cmake --build $build --target goes_support   (rebuilds whatever is out of date; can take long)"
    fi
else
    jobs=$( (command -v nproc >/dev/null 2>&1 && nproc) || echo 2)
    if [ "$quick" = 1 ]; then
        # Only what the patch touched. A plain "make" would also recompile everything that
        # includes a system header updated since SatDump was built, which after a few months
        # of system updates is all of SatDump.
        objects=()
        for name in "${SOURCES[@]}"; do objects+=("$target_dir/goes/hrit/dcs/$name.o"); done
        say "Bringing ${SOURCES[*]} up to date ..."
        make -C "$build" --no-print-directory -f "$target_dir/build.make" -j "$jobs" "${objects[@]}"
        newer=0
        for object in "${objects[@]}"; do [ "$build/$object" -nt "$built" ] && newer=1; done
        if [ "$newer" = 1 ]; then
            say "Linking the plugin ..."
            (cd "$build/plugins/goes_support" && cmake -E cmake_link_script CMakeFiles/goes_support.dir/link.txt)
        else
            say "The plugin in the build folder is already up to date."
        fi
    else
        say "Running the build system for the GOES plugin (this rebuilds whatever is out of date) ..."
        cmake --build "$build" --target goes_support --parallel "$jobs"
    fi
    [ -f "$built" ] || die "the build did not leave $built"
fi

# ------------------------------------------------------------------ the install

step "Install"
if [ "$mode" = check ]; then
    say "Would copy $target to libgoes_support.so$BACKUP_SUFFIX (once), then put the new plugin in its place."
else
    # CMake's own install step for just this plugin, into a scratch folder: it gives the file
    # the same finishing (library search path, permissions) a full install would.
    scratch=$(mktemp -d)
    trap 'rm -rf "$scratch"' EXIT
    DESTDIR=$scratch cmake -P "$build/plugins/goes_support/cmake_install.cmake" >/dev/null
    fresh=$scratch$installed
    [ -f "$fresh" ] || die "CMake's install step did not produce $installed"

    sudo=""
    mkdir -p "$(dirname "$target")" 2>/dev/null || true
    if [ ! -w "$(dirname "$target")" ]; then
        sudo=sudo
        say "Writing to $(dirname "$target") needs sudo."
    fi
    if [ -f "$target" ] && cmp -s "$fresh" "$target"; then
        say "The installed plugin is already this one. Nothing to do."
    else
        if [ -f "$target" ] && [ ! -e "$target$BACKUP_SUFFIX" ]; then
            $sudo cp -p "$target" "$target$BACKUP_SUFFIX"
            say "Kept the old plugin as $target$BACKUP_SUFFIX"
        fi
        # Put in place by renaming, never by writing over the file: a running SatDump has the
        # old one open, and writing into it would crash it.
        $sudo install -m 644 "$fresh" "$target.new.$$"
        $sudo mv -f "$target.new.$$" "$target"
        say "Installed $target"
    fi
fi

if [ "$settings" = 1 ]; then
    step "NOAA's new platform-table address"
    add_address
fi

step "Done"
if [ "$mode" = check ]; then
    say "Nothing was changed. Run again without --check to do it."
elif satdump_running; then
    say "SatDump is running the old plugin until it is restarted: quit SatDump and start it again."
else
    say "Start SatDump. With Parse DCS on, its log should show \"Loading PDTs...\" with no errors after it."
fi
say "To go back:  $0 --undo $src"
