#define _GNU_SOURCE
#include "executor/local.h"
#include "client/terminal.h"
#include "executor/command.h"
#include "executor/container.h"
#include "executor/policy.h"
#include "executor/network.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static json_object *result(const char *status, const char *text, const char *effect)
{
    json_object *value = json_object_new_object();
    json_object_object_add(value, "status", json_object_new_string(status));
    json_object_object_add(value, "text", json_object_new_string(text));
    json_object_object_add(value, "effect", json_object_new_string(effect));
    return value;
}

static bool confirm(const char *command,
    const char *workspace,
    const char *cwd,
    json_object *policy,
    const CommandAuthorization *authorization)
{
    if (!isatty(STDIN_FILENO) || !isatty(STDERR_FILENO)) {
        return false;
    }
    fputs("即将执行：\n----- 命令原文开始 -----\n", stderr);
    if (terminal_write(stderr, (const unsigned char *)command, strlen(command)) != 0) {
        return false;
    }
    fputs("\n----- 命令原文结束 -----\n工作目录：", stderr);
    terminal_write(stderr, (const unsigned char *)workspace, strlen(workspace));
    fputc('/', stderr);
    terminal_write(stderr, (const unsigned char *)cwd, strlen(cwd));
    fprintf(stderr,
        "\n风险等级：%s\n可能风险：",
        json_object_get_int(json_object_object_get(policy, "level")) == 2 ? "中" : "高");
    json_object *risks = json_object_object_get(policy, "risks");
    for (size_t index = 0; index < json_object_array_length(risks); ++index) {
        if (index != 0) {
            fputs("；", stderr);
        }
        fputs(json_object_get_string(json_object_array_get_idx(risks, index)), stderr);
    }
    fputs("\n本次网络授权：", stderr);
    fputs(json_object_to_json_string_ext(authorization->network, JSON_C_TO_STRING_PLAIN), stderr);
    fputs("\n本次凭证授权（值不展示）：", stderr);
    json_object_object_foreach(authorization->environment, name, value)
    {
        (void)value;
        fprintf(stderr, " %s", name);
    }
    for (size_t index = 0; index < json_object_array_length(authorization->files); ++index) {
        json_object *file = json_object_array_get_idx(authorization->files, index);
        fputs("\n只读文件：", stderr);
        const char *source = json_object_get_string(json_object_object_get(file, "source"));
        terminal_write(stderr, (const unsigned char *)source, strlen(source));
        fprintf(stderr,
            " → /run/ragent/credentials/%s",
            json_object_get_string(json_object_object_get(file, "name")));
    }
    if (authorization_has_credentials(authorization)) {
        fputs(
            "\n可能风险：命令能够读取上述凭证，并可能向获准地址发送数据；输出仅本地展示。", stderr);
    }
    fprintf(stderr, "\n执行时限：%u秒\n是否执行？[是/否] ", authorization->timeout_ms / 1000);
    if (fflush(stderr) != 0) {
        return false;
    }
    struct pollfd input = {
        .fd = STDIN_FILENO,
        .events = POLLIN,
    };
    if (poll(&input, 1, (int)command_time_left(authorization->approval_deadline_ms, 900000)) != 1) {
        return false;
    }
    char line[64];
    if (fgets(line, sizeof(line), stdin) == NULL) {
        return false;
    }
    line[strcspn(line, "\r\n")] = '\0';
    return strcmp(line, "是") == 0;
}

static int execution_slot(
    int64_t deadline, CommandMonitor monitor, void *context, const volatile sig_atomic_t *cancelled)
{
    char directory[128];
    snprintf(directory, sizeof(directory), "/run/user/%lu/ragent-policy", (unsigned long)getuid());
    if (mkdir(directory, 0700) != 0 && errno != EEXIST) {
        return -1;
    }
    struct stat info;
    if (lstat(directory, &info) != 0 || !S_ISDIR(info.st_mode) || info.st_uid != getuid()
        || (info.st_mode & 0077) != 0) {
        return -1;
    }
    struct timespec start;
    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        return -1;
    }
    while (!*cancelled && command_time_left(deadline, 10000) > 0
        && (monitor == NULL || monitor(context))) {
        for (unsigned index = 0; index < 4; ++index) {
            char path[160];
            snprintf(path, sizeof(path), "%s/slot-%u", directory, index);
            int fd = open(path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
            if (fd < 0) {
                return -1;
            }
            if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
                return fd;
            }
            int saved = errno;
            close(fd);
            if (saved != EWOULDBLOCK && saved != EINTR) {
                return -1;
            }
        }
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 || now.tv_sec - start.tv_sec >= 10) {
            return -1;
        }
        if (poll(NULL, 0, 20) < 0 && errno != EINTR) {
            return -1;
        }
    }
    return -1;
}

json_object *local_command(const char *command,
    int workspace,
    const char *cwd,
    const char *observer,
    const char *socket,
    LocalTaskReady ready,
    CommandMonitor monitor,
    void *context,
    const volatile sig_atomic_t *cancelled)
{
    struct open_how how = {
        .flags = O_PATH | O_DIRECTORY | O_CLOEXEC,
        .resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS,
    };
    int directory = (int)syscall(SYS_openat2, workspace, cwd, &how, sizeof(how));
    if (directory < 0) {
        return result("failed", "工作目录不存在或超出授权工作区，未执行", "none");
    }
    close(directory);
    char path[64];
    char resolved[4096];
    snprintf(path, sizeof(path), "/proc/self/fd/%d", workspace);
    ssize_t length = readlink(path, resolved, sizeof(resolved) - 1);
    if (length < 0 || (size_t)length >= sizeof(resolved) - 1) {
        return result("failed", "无法固定本地工作目录，未执行", "none");
    }
    resolved[length] = '\0';
    json_object *policy = command_policy(command);
    json_object *denial = json_object_object_get(policy, "denial_reason");
    if (denial != NULL || *cancelled) {
        json_object *error = result("failed",
            denial == NULL ? "任务已取消，未执行" : json_object_get_string(denial),
            "none");
        json_object_put(policy);
        return error;
    }
    if (ready != NULL && !ready(context)) {
        json_object_put(policy);
        return result("cancelled", "任务已过期或连接失效，未执行", "none");
    }
    int level = json_object_get_int(json_object_object_get(policy, "level"));
    char image[72];
    CommandAuthorization authorization;
    authorization_init(&authorization);
    if (level != 1) {
        if (!isatty(STDIN_FILENO) || !isatty(STDERR_FILENO)) {
            json_object_put(policy);
            authorization_destroy(&authorization);
            json_object *value = result("cancelled", "需要交互确认，命令未执行", "none");
            json_object_object_add(value, "code", json_object_new_string("CONFIRMATION_REQUIRED"));
            return value;
        }
        if (!container_resolve_image(socket, image, cancelled)) {
            json_object_put(policy);
            authorization_destroy(&authorization);
            return result("failed", "隔离或命令运行镜像不可用，未执行", "none");
        }
        if (!authorization_timeout(&authorization)
            || (level == 3 && !authorization_prompt(&authorization))) {
            json_object_put(policy);
            authorization_destroy(&authorization);
            return result("cancelled", "本地授权已取消；后续步骤均不得执行", "none");
        }
        if (json_object_array_length(authorization.network) > 0
            && !network_resolve_image(socket, authorization.gateway_image, cancelled)) {
            json_object_put(policy);
            authorization_destroy(&authorization);
            return result("failed", "网络隔离镜像不可用，未执行", "none");
        }
        bool approved = confirm(command, resolved, cwd, policy, &authorization);
        if (!approved || *cancelled || (ready != NULL && !ready(context))) {
            json_object_put(policy);
            authorization_destroy(&authorization);
            return result("cancelled", "用户已取消或任务过期；后续步骤均不得执行", "none");
        }
    }
    json_object_put(policy);
    authorization.deadline_ms = command_now_ms() + (level == 1 ? 10000 : authorization.timeout_ms);
    int slot = execution_slot(authorization.deadline_ms, monitor, context, cancelled);
    if (slot < 0) {
        authorization_destroy(&authorization);
        return result("failed", "本机执行槽已满、等待超时或任务取消，未执行", "none");
    }
    CommandResult captured;
    bool invoked = false;
    bool cleanup_failed = false;
    int status;
    if (level == 1) {
        json_object *request = json_object_new_object();
        json_object_object_add(request, "command", json_object_new_string(command));
        json_object_object_add(request, "workspace", json_object_new_string(resolved));
        json_object_object_add(request, "cwd", json_object_new_string(cwd));
        const char *encoded = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
        char *arguments[] = { (char *)observer, "--workspace-fd", NULL };
        status = command_capture_monitored(observer,
            arguments,
            encoded,
            strlen(encoded),
            65536,
            command_time_left(authorization.deadline_ms, 10000),
            workspace,
            monitor,
            context,
            cancelled,
            &captured);
        json_object_put(request);
    } else {
        ContainerResult outcome;
        status = container_command(command,
            workspace,
            cwd,
            socket,
            image,
            &authorization,
            monitor,
            context,
            cancelled,
            &outcome);
        captured = outcome.command;
        invoked = outcome.invoked;
        cleanup_failed = outcome.cleanup_failed;
    }
    close(slot);
    const char *effect = "none";
    if (invoked) {
        effect = captured.exit_code == 0 && captured.code == NULL ? "committed" : "unknown";
    }
    json_object *value = result(status == 0 ? "success" : "failed", "", effect);
    char *text = terminal_text((const unsigned char *)captured.output, captured.output_length);
    if (text == NULL) {
        json_object_object_add(value, "text", json_object_new_string("执行输出转换失败"));
    } else {
        terminal_write(stderr, (const unsigned char *)captured.output, captured.output_length);
        if (captured.output_length > 0 && captured.output[captured.output_length - 1] != '\n') {
            fputc('\n', stderr);
        }
        json_object_object_add(value,
            "text",
            json_object_new_string(authorization_has_credentials(&authorization)
                    ? "命令已结束；本次使用了凭证，原始输出仅在客户端本地展示，不回传模型"
                    : strlen(text) <= 65536
                    ? text
                    : "命令已结束；转义后的输出超过模型文本上限，原始结果仅在本地展示"));
        free(text);
    }
    json_object_object_add(value, "level", json_object_new_int(level));
    json_object_object_add(value, "exit_code", json_object_new_int(captured.exit_code));
    json_object_object_add(value, "signal", json_object_new_int(captured.signal_number));
    json_object_object_add(value, "cleanup_failed", json_object_new_boolean(cleanup_failed));
    if (captured.code != NULL) {
        json_object_object_add(value, "code", json_object_new_string(captured.code));
    }
    authorization_destroy(&authorization);
    return value;
}
