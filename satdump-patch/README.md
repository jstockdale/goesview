# SatDump: fix for the GOES "Parse DCS" hang

If you receive GOES HRIT with SatDump and have **Parse DCS** switched on, SatDump
can stop writing files a few seconds after it starts: no imagery, no bulletins,
no DCS, while the signal display carries on as if nothing were wrong. This
folder has the fix, a script that installs it, and a workaround for when you
cannot rebuild SatDump.

Until a SatDump release includes the fix, this is how to get it.

## Is this my problem?

Look in SatDump's log (`~/.config/satdump/satdump-*.log`) for these lines, with
nothing about new files after them:

    (I) Loading PDTs...
    (W) Error loading PDT line for DCP processing: stoul
    (I) Loading HADS data...

It affects SatDump built from source since January 2025: 1.2.3 development
builds and 2.0 (master). Release 1.2.2 and older read the tables differently and
do not hang.

## What goes wrong

NOAA moved the list of DCS platforms (the "PDT") in late 2026. The old address
now returns a small "Document Moved" web page, and SatDump saves that page as
the table. When it then reads the "table", the first line fails to parse while
SatDump is holding a lock, the lock is never released, and the thread that
writes every product waits for it for ever.

The fix does four things:

1. The lock is always released, whatever a line contains. This alone ends the
   hang.
2. The table is fetched from NOAA's new address, which sits behind a redirect.
   Redirects are followed only over https, at most five times, with size and
   time limits.
3. A download is kept only if it really is a table (at least 1,000 records).
   A web page or an empty reply never replaces a good file.
4. NOAA's new table is read correctly: it has accented letters, and positions
   in decimal degrees, both of which the old reader got wrong.

## Fix it with the script

You need the SatDump source folder you built your SatDump from, with its build
folder still in it, plus `git`, `cmake` and `make`.

    git clone https://github.com/jstockdale/goesview
    cd goesview/satdump-patch

    ./fix-satdump-dcs.sh --check ~/SatDump      # says what it would do; changes nothing
    ./fix-satdump-dcs.sh ~/SatDump              # does it

Then quit SatDump and start it again. If the script says it did not touch
`settings.json` because SatDump was running, quit SatDump and run
`./fix-satdump-dcs.sh --settings-only` before starting it again.

What the script changes on your machine, and nothing else:

| What | Where | Kept copy |
|---|---|---|
| Three source files patched | `<SatDump>/plugins/goes_support/goes/hrit/dcs/` | `--undo` reverses the patch |
| Two files recompiled, the plugin linked again | `<SatDump>/build/` | |
| The GOES plugin replaced (asks for `sudo`) | where your build installs it, e.g. `/usr/lib/.../satdump/plugins/libgoes_support.so` | `libgoes_support.so.before-dcs-fix` beside it |
| NOAA's new address added to the front of `pdt_urls` | `~/.config/satdump/settings.json`, only while SatDump is closed | `settings.json.before-dcs-fix` |

It only rebuilds the one plugin, which takes a minute or two even on a
Raspberry Pi, and it is safe to run while SatDump is receiving: the new plugin
is put in place by renaming, and SatDump picks it up at its next start. It
stops without changing anything if the installed SatDump was not built in that
build folder, because a plugin built against a different SatDump may crash it.

    ./fix-satdump-dcs.sh --undo ~/SatDump       # put the old plugin and source back

`--help` lists the other options (`--patch-only`, `--full-build`,
`--no-settings`, `--force`).

**How to tell it worked.** After the restart, the log shows `Loading PDTs...`
with no errors after it, and files keep arriving. At the next table update
(every four hours by default) it shows `Updated the PDT: 43400 records`, give
or take.

## Fix it by hand

Use the patch that fits your source:

| Patch | Fits |
|---|---|
| `satdump-1.2.3-dcs.patch` | 1.2.3 development builds, 3 January to 25 May 2025 |
| `satdump-2.0-dcs.patch` | 2.0 (master) from 8 June 2025 |

Source from the two weeks in between fits neither; update it, or make the
changes by hand from either patch.

    cd ~/SatDump
    git apply /path/to/goesview/satdump-patch/satdump-1.2.3-dcs.patch

Build and install SatDump as you normally do. Then, with SatDump closed, add
`https://dadds-dcs-api.data.nesdis.noaa.gov/pdts/PDTS_COMPRESSED.txt` as the
first entry of `plugin_settings` → `goes_support` → `pdt_urls` in
`~/.config/satdump/settings.json` (or in SatDump under Settings → GOES DCS).
SatDump rewrites that file when it closes, so an edit made while it is running
is lost.

## If you cannot rebuild SatDump

This stops the hang without the fix, at the cost of the platform table:

1. Quit SatDump.
2. Delete `~/.config/satdump/PDTS_COMPRESSED.txt`.
3. In `~/.config/satdump/settings.json`, under `plugin_settings` →
   `goes_support`, set `update_interval` to `-1`, so the bad page is not
   fetched again.
4. Start SatDump.

DCS messages are still decoded. Platforms are then named and placed from the
HADS table alone, which covers about 18,000 of them. Undo step 3 once you run
a SatDump that has the fix.

## What has been tested

- The 1.2.3 patch has run on one receiver (SatDump 1.2.3 at `3618db33`,
  aarch64, Debian 12) since 20 November 2026, through every four-hourly table
  update, with no hang and no table errors.
- The 2.0 patch is the same change carried over to 2.0's renamed settings
  calls. It compiles against 2.0 (`9fee0793`) without warnings. It has not
  been run.
- The script has been run end to end on that one machine: Linux, a Makefile
  build. With another build system (Ninja, say) it falls back to an ordinary
  build of the plugin, which works but rebuilds whatever is out of date; that
  path, macOS and Windows are untested.

## Licence

The script is BSD-3-Clause, like the rest of GOESview. The patches change
SatDump and are offered under SatDump's own licence, GPL-3.0.
