#include "client/terminal.h"
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

char *terminal_text(const unsigned char *data, size_t length)
{
    if (length > (SIZE_MAX - 1) / 4) {
        return NULL;
    }
    char *text = malloc(4 * length + 1);
    if (text == NULL) {
        return NULL;
    }
    size_t written = 0;
    for (size_t index = 0; index < length;) {
        unsigned char byte = data[index];
        if ((byte < 32 && byte != '\n' && byte != '\t') || byte == 127) {
            written += (size_t)sprintf(text + written, "\\x%02x", byte);
            ++index;
            continue;
        }
        if (byte < 128) {
            text[written++] = (char)byte;
            ++index;
            continue;
        }
        size_t count = 0;
        uint32_t codepoint = 0;
        if (byte >= 0xc2 && byte <= 0xdf) {
            count = 2;
            codepoint = byte & 0x1f;
        } else if (byte >= 0xe0 && byte <= 0xef) {
            count = 3;
            codepoint = byte & 0x0f;
        } else if (byte >= 0xf0 && byte <= 0xf4) {
            count = 4;
            codepoint = byte & 0x07;
        }
        bool valid = count != 0 && count <= length - index;
        for (size_t part = 1; valid && part < count; ++part) {
            unsigned char continuation = data[index + part];
            valid = continuation >= 0x80 && continuation <= 0xbf;
            codepoint = (codepoint << 6) | (continuation & 0x3f);
        }
        valid = valid && codepoint <= 0x10ffff && !(codepoint >= 0xd800 && codepoint <= 0xdfff)
            && !((count == 2 && codepoint < 0x80) || (count == 3 && codepoint < 0x800)
                || (count == 4 && codepoint < 0x10000));
        if (!valid) {
            written += (size_t)sprintf(text + written, "\\x%02x", byte);
            ++index;
        } else if ((codepoint >= 0x80 && codepoint <= 0x9f)
            || (codepoint >= 0x202a && codepoint <= 0x202e)
            || (codepoint >= 0x2066 && codepoint <= 0x2069) || codepoint == 0x200e
            || codepoint == 0x200f) {
            written += (size_t)sprintf(text + written, "\\u%04x", codepoint);
            index += count;
        } else {
            memcpy(text + written, data + index, count);
            written += count;
            index += count;
        }
    }
    text[written] = '\0';
    return text;
}

int terminal_write(FILE *output, const unsigned char *data, size_t length)
{
    char *text = terminal_text(data, length);
    if (text == NULL) {
        return -1;
    }
    int status = fputs(text, output) < 0 ? -1 : 0;
    free(text);
    return status;
}
