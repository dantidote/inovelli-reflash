#!/usr/bin/env python3
"""Pad an S-record image so every contiguous section starts and ends on a 4-byte boundary.

OpenOCD's efm32 flash driver (EFR32MG21 in the Inovelli VZM31-SN) refuses writes that are not
4-byte aligned, and Inovelli's stock bootloader image has a section starting at 0xBE286. Padding
with 0xFF (the erased-flash value) leaves the programmed result identical. Sections closer than
--merge-gap bytes are joined with 0xFF as well.

usage: python3 align_srec.py in.s37 out.s37 [--merge-gap 256]
"""
import argparse


def parse(path):
    mem, header = {}, None
    with open(path, encoding="ascii", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if line[0] != "S" or len(line) < 10:
                raise SystemExit(f"{path}:{ln}: not an S-record")
            typ, count = line[1], int(line[2:4], 16)
            body = bytes.fromhex(line[4:4 + 2 * count])
            if len(body) != count or (count + sum(body)) & 0xFF != 0xFF:
                raise SystemExit(f"{path}:{ln}: bad length or checksum")
            data = body[:-1]
            if typ == "0":
                header = data[2:]
            elif typ in "123":
                alen = {"1": 2, "2": 3, "3": 4}[typ]
                addr = int.from_bytes(data[:alen], "big")
                for i, b in enumerate(data[alen:]):
                    if mem.get(addr + i, b) != b:
                        raise SystemExit(f"{path}:{ln}: conflicting data at 0x{addr + i:X}")
                    mem[addr + i] = b
    if not mem:
        raise SystemExit(f"{path}: no data records")
    return header, mem


def sections(mem):
    addrs = sorted(mem)
    out, start, prev = [], addrs[0], addrs[0]
    for a in addrs[1:]:
        if a != prev + 1:
            out.append((start, prev + 1))
            start = a
        prev = a
    out.append((start, prev + 1))
    return out


def align(secs, merge_gap):
    out = []
    for s, e in secs:
        s, e = s & ~3, (e + 3) & ~3
        if out and s - out[-1][1] <= merge_gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def record(typ, addr, data):
    body = addr + data
    count = len(body) + 1
    return f"S{typ}{count:02X}{body.hex().upper()}{0xFF - ((count + sum(body)) & 0xFF):02X}\n"


def emit(path, header, mem, secs):
    n = 0
    with open(path, "w", newline="\n") as f:
        f.write(record(0, b"\x00\x00", header or b"aligned"))
        for s, e in secs:
            for a in range(s, e, 32):
                f.write(record(3, a.to_bytes(4, "big"), bytes(mem.get(x, 0xFF) for x in range(a, min(a + 32, e)))))
                n += 1
        f.write(record(7, bytes(4), b""))
    return n


def fmt(secs):
    return ", ".join(f"0x{s:X}-0x{e:X}" for s, e in secs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--merge-gap", type=int, default=256, help="join sections closer than this many bytes (default 256)")
    a = ap.parse_args()

    header, mem = parse(a.src)
    secs = sections(mem)
    aligned = align(secs, a.merge_gap)
    n = emit(a.dst, header, mem, aligned)

    _, back = parse(a.dst)
    bad = [x for x, b in mem.items() if back.get(x) != b]
    pad = [x for x in back if x not in mem]
    if bad or any(back[x] != 0xFF for x in pad):
        raise SystemExit("verification failed; output not trustworthy")
    print(f"{a.src}: {len(mem)} bytes, {len(secs)} section(s): {fmt(secs)}")
    print(f"{a.dst}: {len(back)} bytes, {len(aligned)} section(s), {len(pad)} bytes of 0xFF padding, {n} records: {fmt(aligned)}")


if __name__ == "__main__":
    main()
