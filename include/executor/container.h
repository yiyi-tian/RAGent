#ifndef RAGENT_EXECUTOR_CONTAINER_H
#define RAGENT_EXECUTOR_CONTAINER_H
#include "executor/command.h"
#include "executor/authorization.h"
#include <signal.h>
#include <stdbool.h>

typedef struct {
    CommandResult command;
    bool invoked;
    bool cleanup_failed;
} ContainerResult;

/* 校验本地隔离能力并固定运行镜像身份，不执行用户命令。 */
bool container_resolve_image(
    const char *socket, char image[72], const volatile sig_atomic_t *cancelled);
/* 仅执行本地UI已批准的命令，workspace借用；authorization为冻结的本次授权。
 * authorization为NULL时无网络、无凭证。返回后仍必须检查outcome.cleanup_failed。
 */
int container_command(const char *command,
    int workspace,
    const char *cwd,
    const char *socket,
    const char *expected_image,
    const CommandAuthorization *authorization,
    CommandMonitor monitor,
    void *context,
    const volatile sig_atomic_t *cancelled,
    ContainerResult *outcome);
#endif
