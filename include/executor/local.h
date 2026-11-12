#ifndef RAGENT_EXECUTOR_LOCAL_H
#define RAGENT_EXECUTOR_LOCAL_H
#include <json-c/json.h>
#include <signal.h>
#include <stdbool.h>
#include "executor/command.h"

/* 客户端在确认前后验证任务仍有效；不得由服务端直接提供批准值。 */
typedef bool (*LocalTaskReady)(void *context);

/* 工作区目录借用；工具及socket路径来自本机配置。返回对象由调用者释放。
 * 非观察命令仅从本地TTY读取确认；拒绝会取消整个任务。
 */
json_object *local_command(const char *command,
    int workspace,
    const char *cwd,
    const char *observer,
    const char *socket,
    LocalTaskReady ready,
    CommandMonitor monitor,
    void *context,
    const volatile sig_atomic_t *cancelled);
#endif
