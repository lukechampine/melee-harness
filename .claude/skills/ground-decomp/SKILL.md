---
name: ground-decomp
description: Conventions, style guidelines, and general tips for ground/stage-related code. Use whenever decompiling functions in src/melee/gr/.
---

# Decompiling Ground/Stage Functions

Ground functions implement stage logic: backgrounds, hazards, platforms, and environmental effects. They live in `src/melee/gr/`.

## Key Concepts

### The Ground struct and GET_GROUND

Most ground functions take `Ground_GObj* gobj` as their first parameter. The first thing they do is access the `Ground*` user data. The idiomatic way to write this is `Ground* gp = GET_GROUND(gobj)`. Many also need the JObj: `HSD_JObj* jobj = GET_JOBJ(gobj)`.

Under `#ifdef M2C`, `Ground_GObj` has `user_data` typed as `Ground*`, so m2c may generate `gobj->user_data->field` directly. This doesn't compile in an actual build because `user_data` is really `void*`. Always use `GET_GROUND(gobj)` instead.

### GroundVars: Stage-Specific Variables

Each Ground object has stage-specific state stored in a union at offset 0xC4. In `types.h`, there are two overlapping unions: `gv` (GroundVars) and `u` (GroundVars2). The `gv` union was used historically, but assert statements in the original code reveal the union is actually named `u`. The `gv` members should eventually be migrated to `u`. **If you need to define a new GroundVars struct for a stage, put it in the `u` union (GroundVars2), not `gv`.**

In existing code, you'll see both:
- **`gp->gv.<stage>`** - Older stages: `gp->gv.kongo`, `gp->gv.corneria`, `gp->gv.flatzone`, `gp->gv.castle`, etc.
- **`gp->u.<subtype>`** - Newer/corrected stages: `gp->u.map`, `gp->u.battle`, `gp->u.last`, `gp->u.stadium`, etc.

Follow the convention already used by existing matched functions in the same file.

m2c cannot determine which union field to use and usually guesses wrong. Pass `--union-field GroundVars:<field>` or `--union-field GroundVars2:<field>` to decomp.py to fix this. To determine the correct field, check which GroundVars struct the file uses by looking at existing matched functions in the same file, or at the struct definitions in `src/melee/gr/types.h`.

A single stage file often uses **multiple** GroundVars structs (e.g., `kongo`, `kongo2`, `kongo3`) because each StageCallbacks entry operates on a different Ground object with different state. Look at which callback group the function belongs to in order to determine which GroundVars variant it uses.

### StageCallbacks

Each stage defines an array of `StageCallbacks`, where each entry has 4 function pointers representing one stage object (background layer, hazard, platform, etc.):

```c
typedef struct StageCallbacks {
    void (*callback0)(Ground_GObj*);  // Init: called once during setup
    bool (*callback1)(Ground_GObj*);  // Check: boolean status check (often just returns false)
    void (*callback2)(Ground_GObj*);  // Proc: per-frame update (animation, state machine, physics)
    void (*callback3)(Ground_GObj*);  // Render: display/render callback
    u32 flags;
} StageCallbacks;
```

The callbacks array and how functions map to it is always visible in the same .c file. Use this to understand what role a function plays.

### OnInit Pattern

The `OnInit` function for each stage follows a standard pattern:

```c
static void grStage_OnInit(void)
{
    grXx_stageData = Ground_801C49F8();       // Get stage data pointer
    stage_info.unk8C.b4 = <0 or 1>;           // Stage info flags
    stage_info.unk8C.b5 = <0 or 1>;
    grStage_Setup(0);                          // Setup stage objects by ID
    grStage_Setup(1);
    // ... more Setup calls ...
    Ground_801C39C0();                         // Init collision
    Ground_801C3BB4();                         // Init display
    // Optional: grLib_801C9A10(), camera setup, etc.
}
```

### Common Init Callback (callback0) Pattern

```c
static void grStage_InitCb(Ground_GObj* gobj)
{
    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);
    Ground_801C2ED0(jobj, gp->map_id);        // Setup model from map data
    grAnime_801C8138(gobj, gp->map_id, 0);    // Setup animation
    // Initialize GroundVars fields...
    gp->gv.<stage>.field = value;
}
```

There is a common inline `Ground_JObjInline1` (in `gr/inlines.h`) that combines `Ground_801C2ED0` + `grAnime_801C8138`:
```c
static inline void Ground_JObjInline1(HSD_GObj* gobj)
{
    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);
    Ground_801C2ED0(jobj, gp->map_id);
    grAnime_801C8138(gobj, gp->map_id, 0);
}
```

When you see these two calls back-to-back with no other code between them, you should keep them as explicit calls (not the inline) unless the inline is needed for matching. The inline introduces its own `Ground*` and `HSD_JObj*` variables, which affects register allocation.

### Flags

Ground objects have bitfield flags at `gp->x10_flags` and `gp->x11_flags`:
- `gp->x10_flags.b5 = 1` is commonly set in init callbacks
- `gp->x11_flags.b012 = 2` is commonly set to control rendering priority

m2c doesn't handle bitfields well and may produce ugly bit arithmetic. Replace with the named bitfield accesses.

### Animation Helpers

- `grAnime_801C8138(gobj, gp->map_id, anim_id)` - Setup/play animation
- `grAnime_801C83D0(gobj, start_frame, end_frame)` - Check if animation reached a frame
- `grMaterial_801C94D8(jobj)` - Update material animation
- `grMaterial_801C9604(gobj, param, mode)` - Material transition

### Common Update Helpers

- `Ground_801C2FE0(gobj)` - Update collision
- `Ground_801C2ED0(jobj, map_id)` - Setup model from map
- `Ground_801C2BA4(id)` - Get stage GObj by ID
- `Ground_801C10B8(gobj, callback)` - Register a one-shot callback
- `Ground_801C4D70(gobj, pos, param)` - Set position
- `lb_800115F4()` - Called at end of many update functions

### Static Data and External Pointers

Each stage file typically has:
1. A `static StageCallbacks` array (e.g., `grNBa_803E7DA0[7]`)
2. A `StageData` struct (e.g., `grNBa_803E7E38`) that references the callbacks, file path, and lifecycle functions
3. A static pointer to stage-specific data obtained from `Ground_801C49F8()` (e.g., `grFz_804D6AB0`)

The naming convention for these symbols uses a stage abbreviation prefix: `grNBa_` (Battle), `grKg_` (Kongo), `grCs_` (Castle), `grFz_` (Flat Zone), etc.

## Decompilation Process Examples

### Example: Simple Init Callback

Generate initial guess:
```sh
uv run ./tools/decomp.py grBattle_8021A11C --no-copy --globals=none --no-casts
```
Output:
```c
void grBattle_8021A11C(Ground_GObj* gobj)
{
    Ground* temp_r4;
    HSD_JObj* temp_r3;

    temp_r4 = gobj->user_data;
    temp_r3 = gobj->hsd_obj;
    Ground_801C2ED0(temp_r3, temp_r4->map_id);
    grAnime_801C8138(gobj, temp_r4->map_id, 0);
}
```

Rewrite to use `GET_GROUND` and `GET_JOBJ` macros, and use idiomatic variable names:
```c
static void grBattle_8021A11C(Ground_GObj* gobj)
{
    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);

    Ground_801C2ED0(jobj, gp->map_id);
    grAnime_801C8138(gobj, gp->map_id, 0);
}
```

Compile and check match:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/checkdiff.py grBattle_8021A11C
```
Output:
```diff
--- grbattle.o
+++ grbattle.o
```

Empty diff -- 100% match!

### Example: Init with GroundVars and Flags

Generate initial guess:
```sh
uv run ./tools/decomp.py grBattle_8021A19C --no-copy --globals=none --no-casts
```
Output:
```c
void grBattle_8021A19C(Ground_GObj* gobj)
{
    Ground* temp_r4;
    HSD_JObj* temp_r3;

    temp_r4 = gobj->user_data;
    temp_r3 = gobj->hsd_obj;
    Ground_801C2ED0(temp_r3, temp_r4->map_id);
    grAnime_801C8138(gobj, temp_r4->map_id, 0);
    temp_r4->x11 = (temp_r4->x11 & ~0xE0) | 0x40;
}
```

The bit manipulation `(x11 & ~0xE0) | 0x40` is setting bitfield `x11_flags.b012 = 2`. Rewrite:
```c
static void grBattle_8021A19C(Ground_GObj* gobj)
{
    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);

    Ground_801C2ED0(jobj, gp->map_id);
    grAnime_801C8138(gobj, gp->map_id, 0);
    gp->x11_flags.b012 = 2;
}
```

Compile and check match:
```sh
MELEE_ROOT=~/melee uv run --project ~/melee-harness ~/melee-harness/tools/checkdiff.py grBattle_8021A19C
```
Output:
```diff
--- grbattle.o
+++ grbattle.o
```

Another 100% match!

### Example: Init with Material Animation

Generate initial guess:
```sh
uv run ./tools/decomp.py grBattle_8021A20C --no-copy --globals=none --no-casts
```
Output:
```c
void grBattle_8021A20C(Ground_GObj* gobj)
{
    Ground* temp_r5;
    HSD_JObj* temp_r4;

    temp_r5 = gobj->user_data;
    temp_r4 = gobj->hsd_obj;
    grAnime_801C8138(gobj, temp_r5->map_id, 0);
    grMaterial_801C94D8(temp_r4);
    temp_r5->x11 = (temp_r5->x11 & ~0xE0) | 0x40;
}
```

Note: no `Ground_801C2ED0` call here, but `grMaterial_801C94D8` is used instead. Different callback groups have different initialization needs. Rewrite:
```c
static void grBattle_8021A20C(Ground_GObj* gobj)
{
    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);

    grAnime_801C8138(gobj, gp->map_id, 0);
    grMaterial_801C94D8(jobj);
    gp->x11_flags.b012 = 2;
}
```

### Example: State Machine with Timer (Battlefield Backgrounds)

This is a more complex init callback that sets up a timer-based background rotation:
```c
static void grBattle_8021A344(Ground_GObj* gobj)
{
    u8 _[16];

    Ground* gp = GET_GROUND(gobj);
    HSD_JObj* jobj = GET_JOBJ(gobj);
    gp->x11_flags.b012 = 2;
    gp->u.battle.bg_state = BG_Waiting;
    reset_bg_timer(gp);
    gp->u.battle.curr_bg = -1;
    HSD_JObjSetFlagsAll(jobj, JOBJ_HIDDEN);
}
```

Key observations:
- This uses `gp->u.battle` (GroundVars2 union) because Battlefield uses the `Battlefield` struct
- `u8 _[16]` is stack padding needed for matching (equivalent to `PAD_STACK`)
- Local enums like `BG_Waiting` are defined in the .c file for readability
- Helper inlines like `reset_bg_timer` are common for repeated patterns

### Example: Identifying GroundVars from Context

When decompiling a function, look at the StageCallbacks array to find which group your function belongs to. For example, in `grkongo.c`:

```c
static StageCallbacks grKg_callbacks[] = {
    { grKongo_801D5490, grKongo_801D5574, grKongo_801D557C, grKongo_801D55D4, 0 },
    { grKongo_801D55D8, ..., ..., ..., 0 },  // uses kongo3/kongo2 vars
    // ...
};
```

`grKongo_801D5490` is in callback group 0, which uses `gv.kongo` vars. `grKongo_801D55D8` is in group 1, which uses `gv.kongo3`/`gv.kongo2` vars. If the wrong union field is inferred, the struct field offsets will be wrong. Pass `--union-field GroundVars:kongo` (or whichever variant) to decomp.py.

## Suspicious Pointer Arithmetic = Wrong Types

If m2c (or your matching attempt) produces suspicious pointer arithmetic like `p = (Ground*) ((u8*) p + 4)` to iterate over data, this is a strong signal that one or more types in the function signature are wrong. The correct response is **not** to accept the ugly code -- instead, investigate:

1. **Search for callers** of the function to see what types are actually being passed. Use `rg` or `grep` to find call sites.
2. **Check the function's declaration** in the .h file -- it may have `UNK_PARAMS` or incorrect types.
3. **Update the types** in both the .h declaration and the .c definition, then re-run decomp.py.

For example, if a function is declared as taking `Ground*` but callers pass something else, the struct field offsets will be wrong, leading to manual byte-offset arithmetic to compensate. Fixing the type eliminates the arithmetic.

This applies to any raw byte-offset pointer manipulation -- it almost always means the decompiler is compensating for a type mismatch.

## Style Notes

- Use `GET_GROUND(gobj)` and `GET_JOBJ(gobj)` instead of raw `gobj->user_data` / `gobj->hsd_obj` casts
- Use `gp` for Ground pointer, `jobj` for HSD_JObj pointer
- Replace bit manipulation with named bitfield accesses (e.g., `gp->x11_flags.b012 = 2`)
- Trivial check callbacks that just `return false` are very common -- don't overthink them
- Empty callbacks `void foo(Ground_GObj* arg) {}` are also very common
- The `static` keyword: check if the function is declared in the corresponding .h file. If it's only in the .c file's forward declarations, it should be `static`.
