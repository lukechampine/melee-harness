---
name: easy-funcs
description: Find small, easy-to-decompile functions. Use when looking for simple functions to match or finding low-hanging fruit.
---

# Find Easy Functions

Lists small, undecompiled functions that are likely easy to match.

## Usage

Listing functions smaller than 64 bytes, within a particular file:
```sh
uv run tools/easy_funcs.py -S 52 | grep <filename>
```

Limiting to functions between 40 and 100 bytes, within a particular address range:
```sh
uv run tools/easy_funcs.py -v 80259869 -V 802F6507 -s 40 -S 100
```

## Tips

The script runs `ninja build/GALE01/report.json` internally, then parses this report. This will sometimes trigger a full recompile, which can be slow. It's better to run `ninja` yourself beforehand; that way, you can monitor compilation progress.
