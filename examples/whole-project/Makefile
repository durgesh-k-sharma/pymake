# Whole-project Makefile in the style of Miller's paper, section 7.
# One DAG for the whole tree; each module contributes a small module.mk.

.PHONY: all clean
all: prog

MODULES := ant bee
CFLAGS  += -I. -Wall
SRC     :=
LIBS    :=

# each module adds to SRC (and may add rules)
include $(patsubst %,%/module.mk,$(MODULES))

OBJ := $(SRC:.c=.o)

prog: $(OBJ)
	$(CC) -o $@ $(OBJ) $(LIBS)

# Automatic C include dependencies.  The .d files are *included*, so they
# are remade first and the Makefile is then re-read (paper, section 5.4).
ifneq ($(MAKECMDGOALS),clean)
include $(OBJ:.o=.d)
endif

%.d: %.c
	sh depend.sh `dirname $*.c` $(CFLAGS) $*.c > $@

clean:
	$(RM) prog $(OBJ) $(OBJ:.o=.d) bee/version.h
