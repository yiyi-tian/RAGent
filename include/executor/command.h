#ifndef RAGENT_EXECUTOR_COMMAND_H
#define RAGENT_EXECUTOR_COMMAND_H

#include <signal.h>
#include <stddef.h>

#define COMMAND_OUTPUT_LIMIT 16384U
#define COMMAND_CAPTURE_LIMIT 131072U
#define COMMAND_TIMEOUT_MAX_MS 3000U

typedef struct {
    const char *code;
    int exit_code;
    int signal_number;
    size_t output_length;
    char output[COMMAND_CAPTURE_LIMIT + 1];
} CommandResult;

/* 仅调用方选择好的绝对路径与argv；不解析Shell。result由调用方持有。
 * 超时、取消或输出超限时强杀命令进程组并回收直接子进程。
 * 成功返回0，失败返回-1，具体结果始终写入result。
 */
int command_run(const char *path, char *const argv[], unsigned timeout_ms,
                const volatile sig_atomic_t *cancelled, CommandResult *result);

/* 受控管理进程可提供stdin和更大的响应缓冲；timeout_ms和output_limit由调用者限制。 */
int command_capture(const char *path, char *const argv[], const char *input, size_t input_length,
                    size_t output_limit, unsigned timeout_ms,
                    const volatile sig_atomic_t *cancelled, CommandResult *result);

/* target只能为memory、disk、processes、load；权限与参数白名单在此C边界落实。 */
int command_diagnose(const char *target, unsigned timeout_ms,
                     const volatile sig_atomic_t *cancelled, CommandResult *result);

#endif
