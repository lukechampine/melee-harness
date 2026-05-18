---
name: mismatch-db
description: Knowledge base for common objdiff mismatches. Use to interpret non-empty diffs reported by checkdiff.py.
---

# Common Causes for Match Failure

The following is a list of common reasons why your compiled C code doesn't match the target assembly. 

## Copying structs field-by-field

The diff shows a sequence of loads and stores that differ in type: The target assembly uses `lwz` and `stw`, while the non-matching assembly uses `lfs` and `stfs` (or other load/store pairs).

### Example diff
```diff
@@ -1,10 +1,10 @@ it_80295748
 0x000000: lwz r3 0x2c(r3)
 0x000004: lwz r3 0xc4(r3)
-0x000008: lwz r5 0x4(r3)
+0x000008: lwz r3 0x4(r3)
-0x00000c: lwz r3 0x4(r5)
+0x00000c: lfs f0 0x4(r3)
-0x000010: lwz r0 0x8(r5)
+0x000010: stfs f0 0x0(r4)
-0x000014: stw r3 0x0(r4)
+0x000014: lfs f0 0x8(r3)
-0x000018: stw r0 0x4(r4)
+0x000018: stfs f0 0x4(r4)
-0x00001c: lwz r0 0xc(r5)
+0x00001c: lfs f0 0xc(r3)
-0x000020: stw r0 0x8(r4)
+0x000020: stfs f0 0x8(r4)
 0x000024: blr
```

### Supporting evidence

- Two or more assignments in a row, where each pair of source and destination are offset by one word
- Code involving `Vec3`

## Root cause

When an entire struct is copied, the compiler will copy it word-by-word, without regard to the type of each field. m2c doesn't recognize this, and instead generates code that copies the struct field-by-field, with instructions matching the type of each field.

### Fix

Assign the entire struct in one expression.

### Example fix
```diff
-    pos->x = attrs->x4.x;
-    pos->y = attrs->x4.y;
-    pos->z = attrs->x4.z;
+    *pos = attrs->x4;
```


## `crset` vs `crclr` before a variadic call

The diff shows a `crset` (or `creqv`) instruction at the matching offset where the target has `crclr` (or `crxor`), immediately before a `bl` to a variadic function. The two instructions differ only in their effect on `cr1` bit 6 (`cr1.eq`):

- `crclr 6` / `crxor 6,6,6` clears `cr1.eq` to 0
- `crset 6` / `creqv 6,6,6` sets `cr1.eq` to 1

### Example diff
```diff
 0x000088: addi r5 r1 0x2c
-0x00008c: crclr cr1eq
+0x00008c: crset cr1eq
 0x000090: li r3 0x3e8
 0x000094: bl efSync_Spawn
```

### Supporting evidence

- The `crset`/`crclr` instruction sits immediately before a `bl` to a function whose prototype ends in `...` (e.g. `OSReport`, `printf`, `efSync_Spawn`).
- The arguments being passed at the call site include `f32` or `f64` values (or pointers to them, in the matching case).

### Root cause

The PowerPC SVR4 / EABI ABI uses `cr1.eq` (bit 6 of CR) as a one-bit signal from the caller to a *variadic* callee:

- `crset cr1eq` (=1): the caller has placed at least one floating-point argument in `f1`–`f8`. The callee must save those FPRs into its `va_list` buffer.
- `crclr cr1eq` (=0): no floating-point arguments are in FPRs. The callee can skip the FPR save.

MWCC inspects each argument's type at the call site and decides which one to emit. If your call passes a `f32`/`f64` *value* in the variadic portion, MWCC places it in an FPR and emits `crset`. If every variadic argument is an integer or pointer (including pointers to floats), MWCC emits `crclr`.

The most common cause of this mismatch is that the original C source passed *pointers* to floats but the m2c output dereferenced them, or that a value was passed where the original code passed `&value`. Look at how the variadic callee unpacks its `va_list` — if `va_arg(vlist, ...)` consistently reads pointer types (`Vec3*`, `void*`, `HSD_JObj*`, `f32*`), then the original call site passed pointers, not values.

### Fix

Pass each variadic float argument by address. Often the value can be replaced wholesale by a pointer to a struct field, an existing local, or a stack-spilled copy.

### Example fix
```diff
-    efSync_Spawn(0x3E8, gobj, &pos, dmg, var_f1, var_f2, var_f3, var_f4);
+    efSync_Spawn(0x3E8, gobj, &pos, &dmg);
```

Note that genuinely unused trailing variadic args also disappear in the fix — `efSync_Spawn` only `va_arg`s as many slots as its switch on `gfx_id` requires, so trailing values that the callee never reads should not be passed at all.
