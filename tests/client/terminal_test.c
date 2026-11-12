#include "client/terminal.h"
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int main(void)
{
    const unsigned char controls[] = {'A', 0x1b, '[', '3', '1', 'm', 0xff, 0, '\n'};
    char *text = terminal_text(controls, sizeof(controls));
    assert(text != NULL);
    assert(strcmp(text, "A\\x1b[31m\\xff\\x00\n") == 0);
    free(text);
    const char *unicode = "中文\t\n\xe2\x80\xae\xc2\x9b";
    text = terminal_text((const unsigned char *)unicode, strlen(unicode));
    assert(text != NULL);
    assert(strcmp(text, "中文\t\n\\u202e\\u009b") == 0);
    free(text);
    assert(terminal_text(NULL, SIZE_MAX) == NULL);
    return 0;
}
