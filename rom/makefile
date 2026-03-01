builddir := build
sourcedir := src
datadir := data
distdir := distribution
linkdir := $(builddir)/lnk

romext := sfc
romfile := $(builddir)/SNESVideoPlayer.$(romext)

linker := ./tools/wla-dx-9.5-svn/wlalink/wlalink
linkflags := -dsr
linkobjectfile := $(linkdir)/linkobjs.lst

assembler := ./tools/wla-dx-9.5-svn/wla-65816
assemblerflags := -o

spcasm := ./tools/wla-dx-9.5-svn/wla-spc700
spcflags := -o
spclinker := ./tools/wla-dx-9.5-svn/wlalink/wlalink
spclinkflags := -sb

sound_converter := ./tools/snesbrr-2006-12-13/snesbrr.exe

gfxconverter :=./tools/gracon.py

ifdef USE_SUPERFAMICONV
gfxconverter := ./tools/gfx_converter.py --tool superfamiconv --pad-to-32x32
endif

verify := -verify on
gfx_font_flags := $(verify) -optimize off -palettes 1 -bpp 2 -mode bg
gfx_font4bpp_flags := $(verify) -optimize off -palettes 1 -bpp 4 -mode bg

sfx_normal_flags := -e
sfx_loop_flags := -e -l 0

RD := $(RM) -r
MD := mkdir -p

empty :=
space := $(empty) $(empty)

asmsource := 65816
asmobj := o
asmheader := h

sound := wav
spcsound := brr
spcsource := spc700
spcobject := spcobj
spcbin := $(builddir)/src/object/audio/spc700/apucode.bin

image := png
tile := tiles

sourcefiles := $(shell find $(sourcedir)/ -type f -name '*.$(asmsource)')
objects := $(addprefix $(builddir)/,$(patsubst %.$(asmsource), %.$(asmobj), $(sourcefiles)))
configfiles := $(shell find $(sourcedir)/ -type f -name '*.inc' -o -name '*.opcodes' -o -name '*.registers')
scriptfiles := $(shell find $(sourcedir)/ -type f -name '*.inc' -o -name '*.opcodes' -o -name '*.script')
interfacefiles := $(shell find $(sourcedir)/ -type f -name '*.interface')
inheritancefiles := $(shell find $(sourcedir)/ -type f -name '*.inheritance')

graphics := $(shell find $(datadir)/ -type f -name '*.gfx_font*.$(image)')
converted_graphics := $(sort $(addprefix $(builddir)/,$(patsubst %.$(image), %.$(tile), $(graphics))))

sounds := $(shell find $(datadir)/ -type f -name '*.sfx_normal.$(sound)' 2>/dev/null)
sounds += $(shell find $(datadir)/ -type f -name '*.sfx_loop.$(sound)' 2>/dev/null)
converted_sounds := $(sort $(addprefix $(builddir)/,$(patsubst %.$(sound), %.$(spcsound), $(sounds))))

spcfiles := $(shell find $(sourcedir)/ -type f -name '*.$(spcsource)' 2>/dev/null)
spcobjects := $(addprefix $(builddir)/,$(patsubst %.$(spcsource), %.$(spcobject), $(spcfiles)))
spclinkobjectfile := $(builddir)/src/object/audio/spc700/linkobjs.lst

datafiles := $(converted_graphics) $(converted_sounds) $(spcbin)
builddirs := $(sort $(dir $(objects) $(datafiles) $(spcobjects)) $(linkdir))

#link 65816 objects
all: $(linkobjectfile)
	$(linker) $(linkflags) $(linkobjectfile) $(romfile)
	$(MD) $(distdir)
	cp $(romfile) $(distdir)/SNESVideoPlayer.sfc

#create necessary directory substructure in build directory
$(builddirs):
	$(MD) $@

#create 65816 object linkfile
$(linkobjectfile): $(objects)
	$(shell echo "[objects]" > $(linkobjectfile))
	$(foreach obj, $(objects), $(shell echo "$(obj)" >> $(linkobjectfile)))

#compile 65816 assembler sourcefiles
$(objects): $(builddir)/%.$(asmobj): %.$(asmsource) %.$(asmheader) $(configfiles) $(scriptfiles) $(interfacefiles) $(inheritancefiles) $(datafiles) | $(builddirs)
	$(assembler) $(assemblerflags) $< $@

#convert graphic files
$(converted_graphics): $(builddir)/%.$(tile): %.$(image) | $(builddirs)
	$(gfxconverter) $($(filter gfx_%, $(subst .,$(space), $@))_flags) -infile $< -outfilebase $(patsubst %.$(tile), %, $@)

#convert sound files (WAV to BRR)
$(converted_sounds): $(builddir)/%.$(spcsound): %.$(sound) | $(builddirs)
	$(sound_converter) $($(filter sfx_%, $(subst .,$(space), $@))_flags) $< $@

#compile SPC700 assembler sourcefiles
$(spcobjects): $(builddir)/%.$(spcobject): %.$(spcsource) | $(builddirs)
	$(spcasm) $(spcflags) $< $@

#link SPC700 objects
$(spcbin): $(spcobjects) | $(builddirs)
	$(shell echo "[objects]" > $(spclinkobjectfile))
	$(foreach obj, $(spcobjects), $(shell echo "$(obj)" >> $(spclinkobjectfile)))
	$(spclinker) $(spclinkflags) $(spclinkobjectfile) $(spcbin)

clean:
	$(RD) $(builddir)
	@echo "Build artifacts cleaned"
