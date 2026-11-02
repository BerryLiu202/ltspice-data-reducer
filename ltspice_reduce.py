#!/usr/bin/env python3
"""
ltspice_reduce.py: shrink large LTspice transient exports (.txt) for plotting and analysis.

Modes
  skip    keep every N-th data row (fast, general purpose)
  minmax  in each block of N rows, keep the rows holding the min and max of every
          signal column (preserves peaks, overshoot and ringing extremes)

Both modes always keep the first and last row of every .step segment, never
interpolate (every output row is an original simulation point), and stream the
file line by line, so a 300 MB export needs almost no memory.

No third-party packages required (Python 3.8+).

Usage
  python ltspice_reduce.py data.txt                    # skip mode, N=20
  python ltspice_reduce.py data.txt --mode minmax -n 40
  python ltspice_reduce.py "C:\\path\\to\\folder"        # every .txt in the folder
"""
import argparse
import os
import re
import sys
import time

OUTPUT_TAG = re.compile(r"_(skip|minmax)\d+$")


def parse_row(line):
    try:
        return [float(x) for x in line.split()]
    except ValueError:
        return None


class Reducer:
    def __init__(self, mode, n, writer):
        self.mode, self.n, self.w = mode, n, writer
        self.block = []          # (raw_line, values) for minmax
        self.count = 0           # data rows seen in current segment
        self.last = None         # last raw data line in segment
        self.last_written = None
        self.rows_out = 0
        # local peak tracking: worst error per column over blocks of n rows
        self.blk_n = 0
        self.bo_max, self.bo_min, self.bk_max, self.bk_min = {}, {}, {}, {}
        self.worst = {}

    def _write(self, line):
        if line is not self.last_written:
            self.w.write(line)
            self.last_written = line
            self.rows_out += 1

    def _flush_block(self):
        if not self.block:
            return
        ncol = len(self.block[0][1])
        keep = set()
        for c in range(1, ncol):            # column 0 is time
            vals = [v[c] if len(v) > c else float("nan") for _, v in self.block]
            keep.add(min(range(len(vals)), key=vals.__getitem__))
            keep.add(max(range(len(vals)), key=vals.__getitem__))
        for i in sorted(keep):              # time order preserved
            self._write(self.block[i][0])
        self.block = []

    def _track(self, values, kept):
        for c, x in enumerate(values[1:], start=1):
            self.bo_max[c] = max(self.bo_max.get(c, x), x)
            self.bo_min[c] = min(self.bo_min.get(c, x), x)
            if kept:
                self.bk_max[c] = max(self.bk_max.get(c, x), x)
                self.bk_min[c] = min(self.bk_min.get(c, x), x)
        self.blk_n += 1
        if self.blk_n >= self.n:
            self._close_block()

    def _close_block(self):
        if self.blk_n and self.mode == "skip":
            for c in self.bo_max:
                if c in self.bk_max:
                    err = max(self.bo_max[c] - self.bk_max[c], self.bk_min[c] - self.bo_min[c])
                else:
                    err = self.bo_max[c] - self.bo_min[c]
                self.worst[c] = max(self.worst.get(c, 0.0), err)
        self.blk_n = 0
        self.bo_max, self.bo_min, self.bk_max, self.bk_min = {}, {}, {}, {}

    def data(self, line, values):
        if self.count == 0:
            self._close_block()
        kept = self.count == 0 or (self.mode == "skip" and self.count % self.n == 0)
        self._track(values, kept)
        if self.count == 0:
            self._write(line)               # first row of segment always kept
        elif self.mode == "skip":
            if kept:
                self._write(line)
        else:
            self.block.append((line, values))
            if len(self.block) >= self.n:
                self._flush_block()
        self.count += 1
        self.last = line

    def end_segment(self):
        self._close_block()
        if self.mode == "minmax":
            self._flush_block()
        if self.last is not None:
            self._write(self.last)          # last row of segment always kept
        self.count, self.last = 0, None


def reduce_file(path, mode, n, out_path):
    t0 = time.time()
    rows_in = 0
    # running extremes per column: original vs reduced, for the integrity check
    orig_min, orig_max, red_min, red_max = {}, {}, {}, {}
    header = None

    class TrackingWriter:
        def __init__(self, f):
            self.f = f

        def write(self, line):
            self.f.write(line)
            v = parse_row(line)
            if v:
                for c, x in enumerate(v):
                    red_min[c] = min(red_min.get(c, x), x)
                    red_max[c] = max(red_max.get(c, x), x)

    with open(path, "r", encoding="utf-8", errors="replace") as fin, \
         open(out_path, "w", encoding="utf-8", newline="") as fout:
        r = Reducer(mode, n, TrackingWriter(fout))
        for line in fin:
            if not line.strip():
                continue
            values = parse_row(line)
            if values is None:
                # header or "Step Information: ..." line: close segment, copy through
                r.end_segment()
                fout.write(line)
                r.last_written = line
                if header is None:
                    header = line.split()
                continue
            rows_in += 1
            for c, x in enumerate(values):
                orig_min[c] = min(orig_min.get(c, x), x)
                orig_max[c] = max(orig_max.get(c, x), x)
            r.data(line, values)
        r.end_segment()

    size_in, size_out = os.path.getsize(path), os.path.getsize(out_path)
    print(f"\n{os.path.basename(path)}  ->  {os.path.basename(out_path)}")
    print(f"  rows {rows_in:,} -> {r.rows_out:,}   size {size_in/1e6:.1f} MB -> "
          f"{size_out/1e6:.2f} MB  ({100*size_out/size_in:.1f} %)   {time.time()-t0:.1f} s")
    names = header if header else [f"col{c}" for c in orig_min]
    print("  peak check (original vs reduced):")
    for c in sorted(orig_min):
        if c == 0:
            continue
        name = names[c] if c < len(names) else f"col{c}"
        span = (orig_max[c] - orig_min[c]) or 1.0
        local = 100 * r.worst.get(c, 0.0) / span
        flag = "" if local < 0.5 else f"   <- local peaks clipped by up to {local:.1f} % of range, try --mode minmax"
        print(f"    {name:<18} max {orig_max[c]:.6g} vs {red_max[c]:.6g}   "
              f"min {orig_min[c]:.6g} vs {red_min[c]:.6g}{flag}")


def collect(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.lower().endswith(".txt")]
        else:
            files.append(p)
    # never re-reduce our own outputs
    return [f for f in files if not OUTPUT_TAG.search(os.path.splitext(f)[0])]


def main():
    ap = argparse.ArgumentParser(description="Shrink LTspice transient exports (.txt).")
    ap.add_argument("paths", nargs="+", help=".txt files or folders")
    ap.add_argument("--mode", choices=["skip", "minmax"], default="skip")
    ap.add_argument("-n", type=int, default=20, help="reduction factor (default 20)")
    a = ap.parse_args()
    if a.n < 2:
        sys.exit("-n must be 2 or more")
    files = collect(a.paths)
    if not files:
        sys.exit("No input .txt files found.")
    for f in files:
        stem, _ = os.path.splitext(f)
        reduce_file(f, a.mode, a.n, f"{stem}_{a.mode}{a.n}.txt")


if __name__ == "__main__":
    main()
