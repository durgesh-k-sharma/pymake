SRC += bee/parse.c

# a generated header: the case that breaks recursive make (paper, section 3.2)
bee/version.h: bee/VERSION
	printf '#define VERSION "%s"\n' "$$(cat bee/VERSION)" > $@
