// Test context for void* field type override
typedef int s32;

struct InnerStruct {
    s32 value;
    s32 other;
};

struct OuterStruct {
    s32 id;
    void *data;
};
