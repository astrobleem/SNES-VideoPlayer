.include "src/config/config.inc"

.base BSL
.bank 0 slot 0

;empty — video player has no chapter event data
.section "chapter_event_data" superfree
.ends
