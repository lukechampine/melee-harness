// Test case for void-field-type when the struct is already known
// (field_path is pre-existing, not computed in late_field_path)
typedef int s32;
typedef float f32;

struct InnerData {
    s32 x0;
    s32 x4;
    f32 x8;
    f32 xC;
};

struct Container {
    s32 id;
    void *data;  // Actually InnerData*, but declared as void*
    s32 flags;
};

// Declare the function so arg0 is already typed as Container*
f32 test(struct Container *container);
