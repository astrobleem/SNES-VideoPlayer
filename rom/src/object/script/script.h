.include "src/config/config.inc"

;defines

.def NumOfHashptr 9




.struct vars
  _tmp ds 16
  currPC	dw	;current exec address in script
  buffFlags db	;flags.
  buffBank db		;bank. unused, just for convenience
  buffA	dw
  buffX	dw
  buffY	dw
  buffStack dw	;used to check for stack trashes
.endst

;zp-vars
.enum 0
  iterator INSTANCEOF iteratorStruct
  script INSTANCEOF scriptStruct
  this INSTANCEOF vars
  hashPtr INSTANCEOF oopObjHash NumOfHashptr
  zpLen ds 0
.ende

.def objBrightness hashPtr+12
.def irq.buffer.x this._tmp
.def irq.buffer.y this._tmp+2


;object class static flags, default properties and zero page
.define CLASS.FLAGS OBJECT.FLAGS.Present
.define CLASS.PROPERTIES OBJECT.PROPERTIES.isScript
.define CLASS.ZP_LENGTH zpLen


.base BSL
.bank 0 slot 0


.section "scripts"
.accu 16
.index 16

.include "src/main.script"
.include "src/none.script"
.include "src/videoplayer.script"

.ends
