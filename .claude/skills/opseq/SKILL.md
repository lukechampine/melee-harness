---
name: opseq
description: Find functions that contain a particular sequence of opcodes. Use when you are having trouble decompiling a function and want to find similar already-decompiled functions to compare against.
---

# Opcode Sequence Matching

Find functions that share a specific sequence of opcodes. Useful for finding already-decompiled reference functions and undecompiled candidates that likely have similar structure.

## Usage

### Find decompiled reference functions
```sh
table-typer opseq <comma,separated,opcodes>
```

### Find undecompiled candidates
```sh
table-typer opseq -candidates <comma,separated,opcodes>
```

## Example

If you have an opcode sequence like `beq,mr,bl`:
```sh
table-typer opseq beq,mr,bl
table-typer opseq -candidates beq,mr,bl
```
