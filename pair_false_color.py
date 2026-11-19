#!/usr/bin/env python3
"""Build GOES ABI false-colour images from SatDump's band 2 and band 13 output,
including when the two bands were filed under different timestamps.

SatDump only builds its colour image when both bands sit in the same timestamp
folder. On the GOES HRIT feed one of them sometimes carries the previous
10-minute scan time and lands in a folder of its own, so no colour image is
made. This script pairs each orphaned band with its partner in the neighbouring
folder and writes the colour image next to the later of the two.

Colours come from one of:
  --lut usradioguy   usradioguy.com's custom table (default)
  --lut wxstar       SatDump's stock table
  --lut FILE.png     any SatDump/goestools style 256x256 table
  --lut learn        learn the table from colour images your SatDump already
                     made, so the result matches its current configuration

    # on the receiver, in place: fill in the colour images SatDump skipped
    pair_false_color.py "/path/to/run/IMAGES/GOES-18/Full Disk"

    # from another machine, over an HTTP directory listing
    pair_false_color.py "http://host:1234/run/IMAGES/GOES-18/Full%20Disk" --out ./repaired

Needs numpy and Pillow. Reads PNG bands only.
"""
import argparse
import io
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # full disk frames are large on purpose

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER_FORMAT = "%Y-%m-%d_%H-%M-%S"
BAND_RE = re.compile(r"^G\d+_(\d+)_\d{8}T\d{6}Z\.png$")
SATDUMP_COLOR = "abi_rgb_ABI_False_Color.png"
VISIBLE, INFRARED = 2, 13
LUT_DIRS = [
    os.path.join(HERE, "luts"),
    "/usr/share/satdump/resources/lut/goes/abi",
    "/usr/local/share/satdump/resources/lut/goes/abi",
]
LUT_NAMES = {
    "usradioguy": ["usradioguy.png", "usradioguycustomlut.png"],
    "wxstar": ["wxstar.png", "wxstar/lut.png"],
    "curve": ["ch2_curve.png", "wxstar/ch2_curve.png"],
}


# ---------------------------------------------------------------- colour tables

def find_lut(name):
    """Resolve a table name ("usradioguy", "wxstar", "curve") or a file path."""
    if name not in LUT_NAMES:
        return name if os.path.isfile(name) else None
    for folder in LUT_DIRS:
        for candidate in LUT_NAMES[name]:
            path = os.path.join(folder, candidate)
            if os.path.isfile(path):
                return path
    return None


def lut_table(lut_path, curve_path):
    """256x256x4 table indexed [band 2, band 13], the way SatDump applies a table:
    band 2 goes through the contrast curve and picks the row, band 13 the column.
    Verified pixel-for-pixel against SatDump 1.2 output with the stock table."""
    lut = np.asarray(Image.open(lut_path).convert("RGBA"))
    if lut.shape[:2] != (256, 256):
        raise ValueError(f"{lut_path}: expected a 256x256 table, got {lut.shape[1]}x{lut.shape[0]}")
    curve = np.arange(256)
    if curve_path:
        curve = np.asarray(Image.open(curve_path).convert("L")).ravel()[:256]
    table = lut[curve].copy()
    table[..., 3] = 255
    table[255, 0] = 0  # how SatDump's PNGs encode space around the disk: transparent
    return table


def colourise(table, vis, ir):
    return table[vis, ir]


def load_pair(vis_image, ir_image):
    """Band 2 and band 13 as equal-sized arrays. The coarser band is upscaled:
    on mesoscale sectors band 2 arrives at four times the resolution of band 13."""
    vis_image, ir_image = vis_image.convert("L"), ir_image.convert("L")
    size = max(vis_image.size, ir_image.size)
    if vis_image.size != size:
        vis_image = vis_image.resize(size, Image.BILINEAR)
    if ir_image.size != size:
        ir_image = ir_image.resize(size, Image.BILINEAR)
    return np.asarray(vis_image), np.asarray(ir_image)


def learn_table(src, slots, log):
    """Majority-vote RGBA for every (band 2, band 13) value pair seen in existing
    SatDump colour images. Returns (table, seen)."""
    votes = {}
    for slot in slots:
        log(f"  learning from {slot.name}")
        vis, ir = gray(src, slot, VISIBLE), gray(src, slot, INFRARED)
        rgba = np.ascontiguousarray(np.asarray(src.image(slot.name, SATDUMP_COLOR).convert("RGBA")))
        if not (vis.shape == ir.shape == rgba.shape[:2]):
            log(f"    skipped: image sizes differ {vis.shape} {ir.shape} {rgba.shape[:2]}")
            continue
        colour = rgba.view(np.uint32)[..., 0]
        for row in range(0, vis.shape[0], 256):  # chunked to bound memory
            rows = slice(row, row + 256)
            key = (vis[rows].astype(np.uint64) << 8) | ir[rows]
            packed, counts = np.unique((key << 32) | colour[rows], return_counts=True)
            for p, c in zip(packed.tolist(), counts.tolist()):
                votes[p] = votes.get(p, 0) + c
    if not votes:
        sys.exit("No usable samples to learn the colour table from.")

    packed = np.fromiter(votes.keys(), dtype=np.uint64, count=len(votes))
    counts = np.fromiter(votes.values(), dtype=np.int64, count=len(votes))
    keys = (packed >> 32).astype(np.int64)
    order = np.lexsort((counts, keys))  # by key, most votes last
    keys, packed = keys[order], packed[order]
    winner = np.r_[keys[1:] != keys[:-1], True]

    table = np.zeros(65536, dtype=np.uint32)
    seen = np.zeros(65536, dtype=bool)
    table[keys[winner]] = (packed[winner] & 0xFFFFFFFF).astype(np.uint32)
    seen[keys[winner]] = True
    seen = seen.reshape(256, 256)
    return fill_unseen(table.view(np.uint8).reshape(256, 256, 4), seen), seen


def shifted(a, dy, dx):
    out = np.zeros_like(a)
    h, w = a.shape[:2]
    out[max(dy, 0):h + min(dy, 0), max(dx, 0):w + min(dx, 0)] = \
        a[max(-dy, 0):h + min(-dy, 0), max(-dx, 0):w + min(-dx, 0)]
    return out


def fill_unseen(table, seen):
    """Give value pairs never observed the colour of the nearest observed pair."""
    table, filled = table.copy(), seen.copy()
    while not filled.all():
        prev_filled, prev_table = filled.copy(), table.copy()
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            take = ~filled & shifted(prev_filled, dy, dx)
            table[take] = shifted(prev_table, dy, dx)[take]
            filled |= take
    return table


# ---------------------------------------------------------------------- pairing

class Slot:
    """One timestamp folder: which band files it holds."""

    def __init__(self, name, files):
        self.name = name
        self.time = datetime.strptime(name, FOLDER_FORMAT)
        self.files = set(files)
        self.bands = {}
        for f in files:
            m = BAND_RE.match(f)
            if m:
                self.bands[int(m.group(1))] = f


def find_pairs(slots, window):
    """Every colour image that can be built, as (target, band 2 slot, band 13 slot),
    plus the orphans left over as (slot, missing band).

    Folders holding both bands pair with themselves. A folder holding only one
    is matched to the nearest folder within `window` holding only the other, and
    the image belongs to the later folder (the scan the rest of the bands share).
    """
    pairs, unpaired, used = [], [], set()
    for slot in slots:
        has_vis, has_ir = VISIBLE in slot.bands, INFRARED in slot.bands
        if has_vis and has_ir:
            pairs.append((slot, slot, slot))
        elif has_vis != has_ir and slot.name not in used:
            missing = INFRARED if has_vis else VISIBLE
            near = [s for s in slots
                    if s.name not in used and missing in s.bands
                    and (VISIBLE if has_vis else INFRARED) not in s.bands
                    and abs(s.time - slot.time) <= window]
            if not near:
                unpaired.append((slot, missing))
                continue
            partner = min(near, key=lambda s: abs(s.time - slot.time))
            used.update((slot.name, partner.name))
            vis, ir = (slot, partner) if has_vis else (partner, slot)
            pairs.append((max(vis, ir, key=lambda s: s.time), vis, ir))
    return pairs, unpaired


# -------------------------------------------------------------------------- cli

class Source:
    """A SatDump image-type directory, either local or behind an HTTP listing."""

    def __init__(self, root):
        self.remote = root.startswith(("http://", "https://"))
        self.root = root.rstrip("/")

    def _url(self, *parts):
        return "/".join([self.root] + [urllib.parse.quote(p) for p in parts])

    def listdir(self, sub=None):
        if not self.remote:
            return os.listdir(os.path.join(self.root, sub) if sub else self.root)
        url = (self._url(sub) if sub else self.root) + "/"
        html = urllib.request.urlopen(url, timeout=120).read().decode("utf-8", "replace")
        return [urllib.parse.unquote(h).rstrip("/") for h in re.findall(r'href="([^"?#]+)"', html)]

    def image(self, sub, name):
        if not self.remote:
            return Image.open(os.path.join(self.root, sub, name))
        data = urllib.request.urlopen(self._url(sub, name), timeout=600).read()
        return Image.open(io.BytesIO(data))


def scan(src):
    slots = []
    for name in sorted(src.listdir()):
        try:
            datetime.strptime(name, FOLDER_FORMAT)
        except ValueError:
            continue
        slots.append(Slot(name, src.listdir(name)))
    return slots


def gray(src, slot, band):
    return np.asarray(src.image(slot.name, slot.bands[band]).convert("L"))


def pick_samples(slots, count):
    complete = [s for s in slots
                if SATDUMP_COLOR in s.files and VISIBLE in s.bands and INFRARED in s.bands]
    if len(complete) <= count:
        return complete
    step = (len(complete) - 1) / (count - 1) if count > 1 else 0
    return [complete[round(i * step)] for i in range(count)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help='SatDump "Full Disk" folder, or the URL of its HTTP listing')
    ap.add_argument("--out", help="write results here instead of into the source folders (required for a URL)")
    ap.add_argument("--lut", default="usradioguy", help="usradioguy, wxstar, learn, or a table PNG (default: %(default)s)")
    ap.add_argument("--curve", default="curve", help='band 2 contrast curve PNG, or "none" (default: SatDump\'s)')
    ap.add_argument("--name", help="output file name (default: abi_rgb_ABI_False_Color_<lut>.png)")
    ap.add_argument("--all", action="store_true", help="build every slot, not just those SatDump left without colour")
    ap.add_argument("--window", type=float, default=11, help="max minutes between the two bands (default: %(default)s)")
    ap.add_argument("--limit", type=int, help="only build the newest N images")
    ap.add_argument("--force", action="store_true", help="rebuild images that already exist")
    ap.add_argument("--samples", type=int, default=8, help="with --lut learn: colour images to learn from (default: %(default)s)")
    ap.add_argument("--table", default="false_color_table.npz", help="with --lut learn: cache file (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be built and stop")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)
    src = Source(args.root)
    if src.remote and not args.out:
        ap.error("--out is required when the source is a URL")
    out_root = args.out or src.root
    lut_label = os.path.splitext(os.path.basename(args.lut))[0]
    out_name = args.name or f"abi_rgb_ABI_False_Color_{'paired' if args.lut == 'learn' else lut_label}.png"

    slots = scan(src)
    pairs, unpaired = find_pairs(slots, timedelta(minutes=args.window))
    jobs = [p for p in pairs
            if (args.all or SATDUMP_COLOR not in p[0].files)
            and (args.force or not os.path.exists(os.path.join(out_root, p[0].name, out_name)))]
    jobs.sort(key=lambda j: j[0].time, reverse=True)
    if args.limit:
        jobs = jobs[:args.limit]

    log(f"{len(slots)} folders, {len(jobs)} colour images to build, {len(unpaired)} bands with no partner")
    for slot, missing in unpaired:
        log(f"  no band {missing} within {args.window:g} min of {slot.name}")
    if args.dry_run:
        for target, vis, ir in jobs:
            log(f"  {target.name}: band 2 from {vis.name}, band 13 from {ir.name}")
        return
    if not jobs:
        return

    seen = None
    if args.lut == "learn":
        if os.path.exists(args.table):
            cached = np.load(args.table)
            table, seen = cached["table"], cached["seen"]
            log(f"Using cached colour table {args.table}")
        else:
            samples = pick_samples(slots, args.samples)
            log(f"Learning colour table from {len(samples)} existing colour images")
            table, seen = learn_table(src, samples, log)
            np.savez_compressed(args.table, table=table, seen=seen)
            log(f"  {int(seen.sum())} of 65536 value pairs observed; saved to {args.table}")
    else:
        lut_path = find_lut(args.lut)
        if not lut_path:
            sys.exit(f"Colour table not found: {args.lut}")
        curve_path = None if args.curve == "none" else find_lut(args.curve)
        if args.curve != "none" and not curve_path:
            sys.exit(f"Contrast curve not found: {args.curve}")
        table = lut_table(lut_path, curve_path)
        log(f"Colour table {lut_path}, band 2 curve {curve_path or 'none'}")

    for target, vis_slot, ir_slot in jobs:
        vis, ir = load_pair(src.image(vis_slot.name, vis_slot.bands[VISIBLE]),
                            src.image(ir_slot.name, ir_slot.bands[INFRARED]))
        folder = os.path.join(out_root, target.name)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, out_name)
        Image.fromarray(colourise(table, vis, ir)).save(path + ".tmp", format="PNG")
        os.replace(path + ".tmp", path)
        note = "" if vis_slot is ir_slot else f" (band 2 from {vis_slot.name}, band 13 from {ir_slot.name})"
        if seen is not None:
            note += f" ({100.0 * np.count_nonzero(~seen[vis, ir]) / vis.size:.2f}% of pixels used a nearest-neighbour colour)"
        log(f"{target.name} -> {path}{note}")


if __name__ == "__main__":
    main()
