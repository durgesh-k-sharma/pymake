#!/bin/sh
# From the paper: emit "dir/x.d dir/x.o: dir/x.c headers..."
DIR="$1"
shift 1
case "$DIR" in
"" | ".")
    gcc -MM -MG "$@" | sed -e 's@^\(.*\)\.o:@\1.d \1.o:@'
    ;;
*)
    gcc -MM -MG "$@" | sed -e "s@^\(.*\)\.o:@$DIR/\1.d $DIR/\1.o:@"
    ;;
esac
