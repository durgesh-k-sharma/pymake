#include <stdio.h>
#include "bee/parse.h"
#include "bee/version.h"

int main(void)
{
    printf("prog %s: %d\n", VERSION, parse("41") + 1);
    return 0;
}
