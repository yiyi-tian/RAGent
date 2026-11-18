#ifndef RAGENT_CMD_EXEC_H
#define RAGENT_CMD_EXEC_H

#include <stddef.h>

/**
 * 执行系统命令，并捕获输出
 * @param cmd 要执行的命令名（如 "ls"）
 * @param args 参数数组（必须以 NULL 结尾，如 {"-l", NULL}）
 * @param output 用于存放输出结果的缓冲区
 * @param max_output_len 缓冲区最大长度
 * @return 0 成功，-1 失败
 */
int execute_command_safely(const char *cmd, char *const args[], char *output, size_t max_output_len);

#endif
