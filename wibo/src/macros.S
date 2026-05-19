#include "macros.h"

#if GNU_ASSEMBLER
#define ASM_TYPE(NAME, TYPE) .type NAME, TYPE
#define ASM_END(NAME) .size NAME, .- NAME
#else
#define ASM_TYPE(NAME, TYPE)
#define ASM_END(NAME)
#endif

#define ASM_GLOBAL(NAME, TYPE)                                                                                         \
	.globl SYMBOL_NAME(NAME);                                                                                          \
	ASM_TYPE(SYMBOL_NAME(NAME), TYPE);                                                                                 \
	SYMBOL_NAME(NAME) :
#if GNU_ASSEMBLER
#define ASM_WEAK(NAME, TYPE)                                                                                           \
	.weak SYMBOL_NAME(NAME);                                                                                           \
	ASM_TYPE(SYMBOL_NAME(NAME), TYPE);                                                                                 \
	SYMBOL_NAME(NAME) :
#else
#define ASM_WEAK(NAME, TYPE)                                                                                           \
	.globl SYMBOL_NAME(NAME);                                                                                          \
	.weak_definition SYMBOL_NAME(NAME);                                                                                \
	ASM_TYPE(SYMBOL_NAME(NAME), TYPE)                                                                                  \
	SYMBOL_NAME(NAME) :
#endif

#ifdef __linux__
.section .note.GNU-stack, "", @progbits
#endif

.intel_syntax noprefix

#ifdef __x86_64__

.macro LJMP32 teb_reg
#define m64to32 m64to32_\@
.data
m64to32:
    .long 1f             # 32-bit code offset
    .long 0              # 32-bit code segment (filled in at runtime)
.text
    mov r10w, word ptr [\teb_reg+TEB_CS_SEL]
    mov word ptr [rip+m64to32+4], r10w
    jmp fword ptr [rip+m64to32]
.code32
1:
    endbr32
.endm

.macro LJMP64 teb_reg
#define m32to64_delta m32to64_delta_\@
    push eax
    movzx eax, word ptr [\teb_reg+TEB_HOST_CS_SEL]
    push eax
    call 0f
0:
    pop eax
    .set m32to64_delta, 1f - 0b
    add eax, offset m32to64_delta
    push eax
    retf
.code64
1:
    endbr64
    mov eax, dword ptr [rsp]
    add rsp, 4
.endm

.macro READ_FSBASE out_reg teb_reg
    push rax
    movzx rax, byte ptr [\teb_reg+TEB_HAS_FSGSBASE]
    test rax, rax
    jz 1f
    rdfsbase \out_reg
    jmp 2f
1:
    push rdi
    push rsi
    push rcx
    sub rsp, 8
    mov rsi, rsp    # addr
    mov rdi, 0x1003 # ARCH_GET_FS
    mov rax, 158    # SYS_arch_prctl
    syscall
    pop \out_reg
    pop rcx
    pop rsi
    pop rdi
2:
    pop rax
.endm

.macro WRITE_FSBASE in_reg teb_reg
    push rax
    movzx rax, byte ptr [\teb_reg+TEB_HAS_FSGSBASE]
    test rax, rax
    jz 1f
    wrfsbase \in_reg
    jmp 2f
1:
    push rdi
    push rsi
    push rcx
    mov rsi, \in_reg # addr
    mov rdi, 0x1002  # ARCH_SET_FS
    mov rax, 158     # SYS_arch_prctl
    syscall
    pop rcx
    pop rsi
    pop rdi
2:
    pop rax
.endm

#endif // __x86_64__

.macro GET_TEB_HOST reg

#if defined(__APPLE__) && defined(__x86_64__)
// TLS slot 6 reserved for Win64 compatibility
// https://github.com/apple/darwin-libpthread/blob/03c4628c8940cca6fd6a82957f683af804f62e7f/private/tsd_private.h#L92-L97
mov \reg, gs:[0x30]
#elif defined(__linux__) && defined(__x86_64__)
mov \reg, fs:[currentThreadTeb@tpoff]
#elif defined(__linux__) && defined(__i386__)
mov \reg, gs:[currentThreadTeb@ntpoff]
#else
#error "Unsupported platform"
#endif

.endm
