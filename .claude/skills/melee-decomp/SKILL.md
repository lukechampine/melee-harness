---
name: melee-decomp
description: Decompile individual Melee functions. Use whenever you are decompiling Melee assembly into matching C code.
---

# Decompiling Melee Functions

## Workflow

### 1. Setup

If they were not already provided to you, locate the .c and .h files that will contain the decompiled source for this function using `rg <function_name>`. Usually, there will be a `/// #function_name` placeholder comment in the .c file, and a declaration in the .h file. After locating the file, check if you have any Skills relating to it (such as `item-decomp` for item-related functions).

> **Invoking the tooling.** `decomp.py` is a melee-tree tool — run it from the
> melee checkout (`uv run tools/decomp.py …`). The other scripts
> (`checkdiff.py`, `stack_permute.py`, `permute.py`, `infer_struct.py`,
> `mwcc_dump.py`) live in the sibling harness repo and are run **in place**
> against the melee checkout via `MELEE_ROOT` — there is no symlink/overlay:
> ```sh
> MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/<script>.py …
> ```
> Set `MELEE_ROOT` to your melee checkout. The examples below show the full form.

Run `decomp.py` to get an initial guess:
```sh
uv run tools/decomp.py --no-copy <function name> --globals=none --no-casts
```

Paste the output into the .c file. If there's a `/// #function_name` comment, replace it. Otherwise, append to the end of the file.

### 2. Iterate

Compile and diff against the original:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/checkdiff.py <function_name>
```

An empty diff means a perfect match: You're done!

More likely, you will see a mismatch. Use the diff to reason about why, then edit your code and run `checkdiff.py` again.

**Tip:** If you are decompiling multiple functions, pass `--summary` (or `-s`) along with the function names to `checkdiff.py`. This prints one PASS/FAIL line per function instead of the full diff.

**Tip:** `checkdiff.py` will automatically add any missing header includes. It also attempts to fix (or at least improve) the function's frame size and stack offsets. You can override this behavior by passing `--no-fix-frame`.

### 3. Finish Up

Success means ONE thing only: the compiled output matches the original assembly byte-for-byte (or instruction-for-instruction, depending on project standards). Close is not good enough. 99% is failure. As such, failure is quite likely, particularly for large functions. You have a maximum of 5 serious attempts to achieve a match. Each attempt should be meaningfully different based on what you learned from the previous failure.

If you do not succeed after 5 attempts, give up. Do not delete your code; leave it as-is for the user to improve later. You do not need to respond with detailed information about the failure, or placate the user with assurances that the implementation is semantically correct. The user is already well-aware of the common causes of failure. Example failure responses:
```
BAD:
The main blocker for the non-matching functions is MWCC's register allocation ordering, which is sensitive to variable declaration order, assignment order, and stack layout. The logic is correct in all cases - the differences are purely register swaps and stack offset shifts.

GOOD:
The non-matching functions all have regswaps that I wasn't able to resolve.
```

After verifying that you have a 100% match, do a final style check to ensure that your function follows conventions and uses appropriate idioms. Of course, preserving the 100% match is of paramount importance, so only make changes that don't break the match, and revert to the 100% matching version if you accidentally break it.

## Understanding m2c Behavior

`decomp.py` uses m2c under the hood. Understanding the various quirks of m2c is very important to decompilation work.

If the function being decompiled has been declared in a header, m2c will use the parameter and return types of that declaration. These types are often wrong. In particular, most un-decompiled functions are declared as `UNK_RET foo(UNK_PARAMS)`, aka `s32 foo(void)`. Updating these declarations with more accurate types will yield much better m2c output.

m2c often generates functions that use a different amount of stack than the original. This can be fixed with the `PAD_STACK` macro.

m2c cannot detect when a function has been inlined. However, in this codebase, there is a telltale sign of inlining: The presence of multiple `Item*`, `Ground*`, or `Fighter*` variables in the same function body. In virtually every instance, this indicates that a function should be factored out, with the other Item/Ground/Fighter declaration occurring at the top of the helper function. Since inlining a function can have unpredictable effects on registers and stack usage, identifying inlined functions accurately is enormously important for getting good matches, and often benefits multiple functions. Always remember that we are attempting to recover code that was originally written by humans -- and humans love helper functions.

If a struct field is being accessed, but the definition of that struct does not have a field at the necessary offset, m2c will generate an access to a (non-existent) "unk" field, e.g. `foo->unkC`. This indicates that either the definition of `foo` is inaccurate, or that `foo` is the wrong type.

When a union is accessed, m2c must guess which member to use, and usually guesses wrong. If you know which member should be used, append `--union-field <union type>:<member>` to the `decomp.py` command.

When a void* is accessed, m2c must guess which type to cast it to, and usually guesses wrong. If you know which type should be used, append `--void-field-type <Struct.field>:<type>` to the `decomp.py` command.

m2c cannot determine when the stack should contain a struct type, such as `Vec3`; instead, it emits separate declarations for each field of the struct. When this happens, try passing `--stack-structs`, which will output the inferred stack types as a C struct. Then rewrite this struct to use better types, append it to the end of ./build/ctx.c, and rerun `decomp.py`.

m2c cannot distinguish between aliases for the same type. For example, it often generates `s32` instead of `bool`, and `Point3d` instead of `Vec3`. Look at surrounding code to infer which alias to use.

Some types have alternate definitions for m2c, using `#ifdef M2C`. This enables better type resolution, but can also result in code that doesn't compile. For example, `Item_GObj` is normally equivalent to `HSD_GObj`, but has an alternate definition where the `user_data` field is an `Item*` instead of `void*`. This can cause m2c to generate expressions like `user_data->xC4_article_data`, which does not compile in an actual build because `user_data` is really `void*`.

m2c doesn't understand bitfields very well. If you see ugly-looking bit arithmetic code, check whether it's accessing a struct with bitfields; if so, you may be able to replace this code with simple bit toggles.

m2c doesn't understand `<=` or `>=` comparisons. It will output something like `M2C_ERROR(/* unknown instruction: cror eq, lt, eq */)`, followed by an expression with `==`. When this happens, replace the `==` with `<=` or `>=` as indicated by the error message.

Another error, `Read from unset register`, may occur in two cases. First, when a `void` function is called, but `$r3` is read afterward; this indicates that the `void` function is not `void` after all, and must be updated to return a value. Second, when the function is missing parameters; for example, you may need to add a `f32` parameter if the function reads the "unset" `$f0` register.

m2c will automatically decode symbols for you (mainly floats and strings), but if you're working on a preexisting function, you may need to locate the symbols yourself. To do this, search for the symbol's `.obj` directive in the corresponding `build/GALE01/asm/<path>.s` file.

## MWCC Quirks

MWCC is picky about types when it comes to loop unrolling. Specifically, the loop variable *must* have type `int`. Everywhere else, we use `s32`, but if you use `s32` (or any other type) for a loop variable, it won't be unrolled. Therefore, always use `int` whenever you write a `for` loop.

Semantically-equivalent C code will often compile to different assembly. If you're struggling to get a match, here are some things to try.

- Replace an `if` statement with a ternary expression, or vice versa.
- Replace an `if` statement that computes absolute value with the `ABS` macro.
- Replace a `!= 0` (or `!= NULL`) comparison with a simple truthiness check, or vice versa.
- Introduce a new variable to break up a complex expression, or eliminate an existing variable to combine simple expressions.
- Replace `* 0.5` with `/ 2`, or vice versa.
- Inline an assignment into an expression, or extract an existing assignment expression into a statement.

If you get stuck, look around in the file for functions that contain logic similar to the function you're working on. If you find a close match, there's a good chance that one of the functions calls the other, or that they both call a shared helper function. Refactoring the code in this way is likely to yield a better match, and should be strongly preferred even if it temporarily reduces the match percentage.

## Additional Tools at Your Disposal

### Brute-forcing matches

One of the most common sources of mismatches is an incorrect frame size, and/or variables being placed at incorrect stack offsets. `checkdiff.py` will fix trivial stack issues automatically; it does so by running `stack_permute.py --fix-frame`. If the stack issues are more complex, you can try fixing them via brute force, with the stack permuter:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/stack_permute.py <function name> --timeout 10
```
This will attempt various permutations of the stack layout until it finds a match, up to a specified timeout. Note that the permuter is very CPU intensive, and should therefore be used judiciously. Only use it for functions that are close to matching, with the remaining differences mostly relating to stack. Also refrain from setting a timeout longer than 30 seconds.

Other minor mismatches (such as regswaps) can sometimes be resolved by randomly permuting function logic. This is facilitated by the more general `permute.py` script:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/permute.py <function name> --timeout 60
```

This will rapidly test random permutations for up to 60 seconds, then output a diff for the best result. Since the permuter works on preprocessed C, the diff will typically not cleanly apply as-is; you must apply it manually.

As with `stack_permute.py`, you should only use the permuter for functions that are close to matching. For example, do not use the permuter when the checkdiff.py output shows additional or missing instructions, as this is likely to be a waste of time. Also refrain from setting a timeout longer than 60 seconds.

By default, `permute.py` only permutes the specified function. Read its doccomment to learn more advanced invocation patterns, such as permuting helper functions.

### Determining struct field types

Proper types are essential to the decompilation process. For this reason, type-erasing casts (such as `(void*)`, `(u8*)`, or the `M2C_FIELD` macro) are strongly discouraged. Determining these types is often a process of trial and error, but there is a tool, `infer_struct.py`, that can automate the process somewhat. It scans the function's asm, watches every load/store that uses a particular register, and prints a candidate struct definition with offsets, sizes, and types inferred from the instruction mnemonics. It also detects stride hints to suggest the element size of an iterated struct. For example, when decompiling `grKinokoRoute_80207B5C`:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/infer_struct.py grKinokoRoute_80207B5C r29
```
Output:
```c
struct grKinokoRoute_80207B5C_arg {
    /* +0x08 */ u32            x8;
    /* +0x0c */ u32            xC;
    /* +0x10 */ u8             x10;
    char pad_00[0x3]; /* +0x11 */
    /* +0x14 */ u32            x14;
    char pad_01[0xac]; /* +0x18 */
    /* +0xc4 */ u8             xC4;
    char pad_02[0x1]; /* +0xc5 */
    /* +0xc6 */ u16            xC6;
    /* +0xc8 */ u16            xC8;
    /* +0xca */ u16            xCA;
    /* +0xcc */ u16            xCC;
    char pad_03[0x2]; /* +0xce */
    /* +0xd0 */ f32            xD0;
    /* +0xd4 */ u32            xD4;
    /* +0xd8 */ u32            xD8;
};
```
Here, fields up to +0x14 are part of the `Ground` struct itself; the GroundVars-relative fields start at +0xC4. Skimming the asm to confirm xC4 is bitfield-accessed (`lbz`/`stb` with `rlwimi`) and xD0/xD4/xD8 are a Vec3 (3 consecutive stores from an inlined `HSD_JObjGetTranslation`) yields the correct GroundVars layout:
```c
struct grKinokoRoute_GroundVars2 {
    /* gp+C4 */ UnkFlagStruct xC4_flags;
    /* gp+C5 */ u8 xC5_pad;
    /* gp+C6 */ s16 xC6;
    /* gp+C8 */ s16 xC8;
    /* gp+CA */ s16 xCA;
    /* gp+CC */ s16 xCC;
    /* gp+CE */ u8 xCE_pad[2];
    /* gp+D0 */ Vec3 xD0;
};
```
This struct was then added to the appropriate union in `gr/types.h`, enabling some preexisting type-erasing casts to be replaced proper field accesses.

`infer_struct.py` works best when the seed register holds the pointer for the whole function (a function arg, or a loop iterator initialized once). When the function reseeds the register (e.g. `addi r30, r3, ...` after a `bl`), the tool automatically reseeds it too. For functions where the pointer is computed via `add rD, rA, rB` (combining a base loaded from memory with an iteration offset), `infer_struct.py` can't follow the chain and will only report the stride hint, not the field offsets — try a different register, or fall back to reading the asm by hand. Lastly, note that you can pass `-v` to see every observed access with its source address.

### Determining a constant's value

Decoded constant values live in the assembly dumps under `build/GALE01/asm/`. For example:

```sh
grep -A2 '^\.obj it_804DC6DC' build/GALE01/asm/melee/it/itcoll.s
# .obj it_804DC6DC, global
#     .float -1
# .endobj it_804DC6DC
```
