#ifndef RAGENT_CLIENT_TERMINAL_H
#define RAGENT_CLIENT_TERMINAL_H
#include <stddef.h>
#include <stdio.h>

/* 将控制字符、无效UTF-8及双向控制字符转为可见转义，不截断正文。调用者free返回值。 */
char *terminal_text(const unsigned char *data, size_t length);
int terminal_write(FILE *output, const unsigned char *data, size_t length);
#endif
