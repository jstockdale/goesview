#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Hearing about new files in a folder without listing it.

SatDump writes its text bulletins and DCS files into one flat folder each, and a busy day
puts tens of thousands of files there. Listing such a folder to find the handful that are
new costs real processor time (half a second for 26,000 files on an exFAT disk), and doing
that every few seconds adds up to a load that grows all day long.

On Linux the kernel can report each new name as it appears (inotify), which costs nothing
per file already there. This module is a thin wrapper round that, through ctypes so it needs
nothing installed. Where it is not available, start() returns None and the callers go back to
listing, at a pace matched to what a listing costs (see budget()).

A watch only hears changes made on this machine. A folder on a network drive that another
machine writes to stays silent, so callers also compare the folder's modification time and
stop trusting a watch that said nothing while the folder changed.
"""

import ctypes
import os
import stat
import struct
import sys
import threading

IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x008, 0x040, 0x080, 0x100, 0x200
IN_Q_OVERFLOW, IN_IGNORED, IN_ONLYDIR = 0x4000, 0x8000, 0x01000000
ARRIVED = IN_CREATE | IN_MOVED_TO
MASK = ARRIVED | IN_DELETE | IN_MOVED_FROM | IN_ONLYDIR
HEADER = struct.Struct("iIII")  # watch, mask, cookie, length of the name that follows
QUICK = 0.05  # a listing faster than this (seconds) is not worth rationing


def budget(cost, share=0.05, least=0.0):
    """How long to leave between listings that take `cost` seconds each, so that listing
    takes no more than `share` of one processor. Quick listings are not held back at all."""
    return least if cost < QUICK else max(least, cost / share)


class Named:
    """A file known by name, from a watch: stands in for the entry a listing would give."""

    __slots__ = ("name", "path")

    def __init__(self, folder, name):
        self.name, self.path = name, os.path.join(folder, name)

    def stat(self):
        return os.stat(self.path)

    def is_dir(self, follow_symlinks=True):
        try:
            return stat.S_ISDIR((os.stat if follow_symlinks else os.lstat)(self.path).st_mode)
        except OSError:
            return False


class Watcher:
    """Folders whose new files the kernel reports. Safe to use from several threads."""

    def __init__(self):
        libc = ctypes.CDLL(None, use_errno=True)
        self._add, self._rm = libc.inotify_add_watch, libc.inotify_rm_watch
        self._add.argtypes, self._add.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
        self._rm.argtypes, self._rm.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int
        libc.inotify_init1.argtypes, libc.inotify_init1.restype = [ctypes.c_int], ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify is not available")
        self.lock = threading.Lock()
        self.paths = {}    # watch number -> folder
        self.numbers = {}  # folder -> watch number

    def add(self, path):
        """Start hearing about `path`. False if the system will not watch it."""
        with self.lock:
            if path in self.numbers:
                return True
            number = self._add(self.fd, os.fsencode(path), MASK)
            if number < 0:
                return False
            self.paths[number], self.numbers[path] = path, number
            return True

    def drop(self, path):
        with self.lock:
            number = self.numbers.pop(path, None)
            if number is not None:
                self.paths.pop(number, None)
                self._rm(self.fd, number)

    def watching(self, path):
        with self.lock:
            return path in self.numbers

    def read(self):
        """What has happened since the last call: ({folder: [new names, in order]},
        {folders in which anything changed}, lost).

        `lost` is True when the kernel had more to say than it could queue: some names were
        dropped, so every watched folder needs listing once."""
        chunks = []
        while True:
            try:
                chunk = os.read(self.fd, 1 << 16)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        news, touched, lost = {}, set(), False
        with self.lock:
            for data in chunks:
                at = 0
                while at + HEADER.size <= len(data):
                    number, mask, _, length = HEADER.unpack_from(data, at)
                    name = data[at + HEADER.size:at + HEADER.size + length].split(b"\0", 1)[0]
                    at += HEADER.size + length
                    if mask & IN_Q_OVERFLOW:
                        lost = True
                        continue
                    path = self.paths.get(number)
                    if path is None:
                        continue
                    if mask & IN_IGNORED:  # the folder itself is gone: the kernel has stopped watching
                        del self.paths[number]
                        self.numbers.pop(path, None)
                        continue
                    touched.add(path)
                    if mask & ARRIVED and name:
                        news.setdefault(path, []).append(os.fsdecode(name))
        return news, touched, lost

    def close(self):
        with self.lock:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
            self.paths.clear()
            self.numbers.clear()


def start():
    """A Watcher, or None where the system has no way to report new files."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return Watcher()
    except (OSError, AttributeError, ValueError):
        return None
