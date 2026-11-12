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

/* 只执行已经由本地UI批准的命令；无网络、无凭证，目录借用且必须为授权工作区。 */
bool container_resolve_image(
    const char *socket, char image[72], const volatile sig_atomic_t *cancelled);
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
