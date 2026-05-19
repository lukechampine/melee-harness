.set noat
.set noreorder

glabel test
/* 000000 00400000 8C880004 */  lw    $t0, 4($a0)        # Load container->data (void*)
/* 000004 00400004 C500000C */  lwc1  $f0, 0xC($t0)      # Load data->xC (float at offset 0xC)
/* 000008 00400008 03E00008 */  jr    $ra
/* 00000C 0040000C 00000000 */   nop
