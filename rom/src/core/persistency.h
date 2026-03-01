.include "src/config/config.inc"

; Stubbed persistency — no SRAM needed for video player

.ramsection "persistency zero page" bank 0 slot 2
core.persistency.zp.start ds 0
core.persistency.zp.end ds 0
.ends

.base BSL
.bank 0 slot 0
