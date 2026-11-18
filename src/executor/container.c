#define _GNU_SOURCE
#include "executor/container.h"
#include "executor/policy.h"
#include "executor/owner.h"
#include "executor/network.h"
#include "protocol/protocol.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <sys/syscall.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <unistd.h>

#define ARGUMENT_LIMIT 1024U

typedef struct {
    char *values[ARGUMENT_LIMIT];
    size_t count;
} Arguments;

static int append(Arguments *arguments, const char *text)
{
    if (arguments->count >= ARGUMENT_LIMIT - 1) {
        errno = E2BIG;
        return -1;
    }
    char *value = strdup(text);
    if (value == NULL) {
        return -1;
    }
    arguments->values[arguments->count++] = value;
    return 0;
}

static void destroy_arguments(Arguments *arguments)
{
    for (size_t index = 0; index < arguments->count; ++index) {
        free(arguments->values[index]);
    }
}

static int docker(const char *socket,
    char *const command[],
    unsigned timeout,
    int64_t deadline,
    const volatile sig_atomic_t *cancelled,
    CommandResult *result)
{
    char endpoint[512];
    if (strlen(socket) > 480) {
        return -1;
    }
    snprintf(endpoint, sizeof(endpoint), "unix://%s", socket);
    char *argv[ARGUMENT_LIMIT] = { "docker", "--host", endpoint };
    size_t used = 3;
    for (size_t index = 0; command[index] != NULL; ++index) {
        if (used >= ARGUMENT_LIMIT - 1) {
            return -1;
        }
        argv[used++] = command[index];
    }
    return command_capture("/usr/bin/docker",
        argv,
        NULL,
        0,
        65536,
        command_time_left(deadline, timeout),
        cancelled,
        result);
}

static bool rootless(const char *socket, int64_t deadline, const volatile sig_atomic_t *cancelled)
{
    struct stat info;
    if (stat(socket, &info) != 0 || !S_ISSOCK(info.st_mode) || info.st_uid != getuid()
        || getuid() == 0) {
        return false;
    }
    CommandResult result;
    char *command[] = { "info", "--format", "{{json .}}", NULL };
    if (docker(socket, command, 5000, deadline, cancelled, &result) != 0) {
        return false;
    }
    json_object *object
        = json_parse_object((const unsigned char *)result.output, result.output_length);
    if (object == NULL) {
        return false;
    }
    json_object *options = json_object_object_get(object, "SecurityOptions");
    bool found = false;
    bool seccomp = false;
    if (json_object_is_type(options, json_type_array)) {
        for (size_t index = 0; index < json_object_array_length(options); ++index) {
            const char *value = json_object_get_string(json_object_array_get_idx(options, index));
            found = found || strncmp(value, "name=rootless", 13) == 0;
            seccomp = seccomp || strncmp(value, "name=seccomp", 12) == 0;
        }
    }
    const char *version = json_object_get_string(json_object_object_get(object, "CgroupVersion"));
    const char *driver = json_object_get_string(json_object_object_get(object, "CgroupDriver"));
    bool ok = found && seccomp && version != NULL && strcmp(version, "2") == 0 && driver != NULL
        && strcmp(driver, "systemd") == 0;
    json_object_put(object);
    return ok;
}

static char *mount_spec(const char *source, const char *target, bool readonly)
{
    size_t capacity = 2 * (strlen(source) + strlen(target)) + 80;
    char *spec = malloc(capacity);
    if (spec == NULL) {
        return NULL;
    }
    strcpy(spec, "type=bind,\"source=");
    char *output = spec + strlen(spec);
    for (const char *input = source; *input != '\0'; ++input) {
        if (*input == '"') {
            *output++ = '"';
        }
        *output++ = *input;
    }
    strcpy(output, "\",\"target=");
    output += strlen(output);
    for (const char *input = target; *input != '\0'; ++input) {
        if (*input == '"') {
            *output++ = '"';
        }
        *output++ = *input;
    }
    strcpy(output, readonly ? "\",readonly" : "\"");
    return spec;
}

/* 命令不能通过未遮蔽的硬链接改写工作区外的同一inode。 */
static int masks(Arguments *arguments,
    int directory,
    const char *relative,
    const char *empty,
    const char *blocked,
    const struct stat *trusted,
    unsigned depth)
{
    if (depth > 256) {
        errno = E2BIG;
        return -1;
    }
    int copy = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (copy < 0) {
        return -1;
    }
    DIR *stream = fdopendir(copy);
    if (stream == NULL) {
        close(copy);
        return -1;
    }
    int status = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(stream);
        if (entry == NULL) {
            if (errno != 0) {
                status = -1;
            }
            break;
        }
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        char path[4096];
        int length = snprintf(path, sizeof(path), "%s/%s", relative, entry->d_name);
        struct stat info;
        if (length < 0 || (size_t)length >= sizeof(path)
            || fstatat(directory, entry->d_name, &info, AT_SYMLINK_NOFOLLOW) != 0) {
            status = -1;
            break;
        }
        bool protected = policy_protected_path(entry->d_name)
            || (S_ISREG(info.st_mode) && info.st_nlink > 1)
            || (!S_ISREG(info.st_mode) && !S_ISDIR(info.st_mode) && !S_ISLNK(info.st_mode))
            || (info.st_dev == trusted->st_dev && info.st_ino == trusted->st_ino);
        if (protected && !S_ISLNK(info.st_mode)) {
            char target[8192];
            if (snprintf(target, sizeof(target), "/workspace%s", path) >= (int)sizeof(target)) {
                status = -1;
                break;
            }
            char *spec = mount_spec(S_ISDIR(info.st_mode) ? empty : blocked, target, true);
            if (spec == NULL) {
                status = -1;
                break;
            }
            status = append(arguments, "--mount");
            if (status == 0) {
                status = append(arguments, spec);
            }
            free(spec);
            if (status != 0) {
                break;
            }
        } else if (S_ISDIR(info.st_mode)) {
            int child
                = openat(directory, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) {
                status = -1;
                break;
            }
            status = masks(arguments, child, path, empty, blocked, trusted, depth + 1);
            close(child);
            if (status != 0) {
                break;
            }
        }
    }
    closedir(stream);
    return status;
}

bool container_resolve_image(
    const char *socket, char image[72], const volatile sig_atomic_t *cancelled)
{
    if (!rootless(socket, 0, cancelled)) {
        return false;
    }
    CommandResult result;
    char *inspect[] = {
        "image",
        "inspect",
        "ragent-command-sandbox:v2",
        "--format",
        "{{.Id}}",
        NULL,
    };
    if (docker(socket, inspect, 5000, 0, cancelled, &result) != 0) {
        return false;
    }
    result.output[strcspn(result.output, "\r\n")] = '\0';
    if (strlen(result.output) != 71 || strncmp(result.output, "sha256:", 7) != 0) {
        return false;
    }
    memcpy(image, result.output, 72);
    return true;
}

static int failed(CommandResult *result, const char *text)
{
    memset(result, 0, sizeof(*result));
    result->code = "COMMAND_NOT_ALLOWED";
    result->exit_code = -1;
    result->output_length = strlen(text);
    memcpy(result->output, text, result->output_length + 1);
    return -1;
}

int container_command(const char *command,
    int workspace,
    const char *cwd,
    const char *socket,
    const char *expected_image,
    const CommandAuthorization *authorization,
    CommandMonitor monitor,
    void *context,
    const volatile sig_atomic_t *cancelled,
    ContainerResult *outcome)
{
    memset(outcome, 0, sizeof(*outcome));
    CommandResult *result = &outcome->command;
    int64_t deadline = authorization != NULL && authorization->deadline_ms > 0
        ? authorization->deadline_ms
        : command_now_ms() + (authorization == NULL ? 120000 : authorization->timeout_ms);
    if (!rootless(socket, deadline, cancelled)) {
        return failed(result, "rootless隔离、seccomp或cgroup不可用，未执行");
    }
    struct stat root;
    if (fstat(workspace, &root) != 0 || !S_ISDIR(root.st_mode)) {
        return failed(result, "授权工作区不可用，未执行");
    }
    struct open_how directory_how = {
        .flags = O_PATH | O_DIRECTORY | O_CLOEXEC,
        .resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS,
    };
    int selected = (int)syscall(SYS_openat2, workspace, cwd, &directory_how, sizeof(directory_how));
    if (selected < 0) {
        return failed(result, "命令工作目录超出授权范围或不可访问，未执行");
    }
    close(selected);
    char executable[4096];
    ssize_t executable_length = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    struct stat trusted;
    if (executable_length < 0 || (size_t)executable_length >= sizeof(executable) - 1) {
        return failed(result, "无法固定执行器身份，未执行");
    }
    executable[executable_length] = '\0';
    char *parent = strrchr(executable, '/');
    if (parent == NULL) {
        return failed(result, "执行器安装路径非法，未执行");
    }
    *parent = '\0';
    if (stat(executable, &trusted) != 0
        || (trusted.st_dev == root.st_dev && trusted.st_ino == root.st_ino)) {
        return failed(result, "不能将执行器安装目录本身作为工作区，未执行");
    }
    json_object *policy = command_policy(command);
    bool valid = json_object_get_int(json_object_object_get(policy, "level")) >= 2
        && json_object_object_get(policy, "denial_reason") == NULL;
    json_object_put(policy);
    if (!valid) {
        return failed(result, "该入口仅接受通过策略分析且已获本地批准的容器命令");
    }
    CommandResult captured;
    char *inspect[] = {
        "image",
        "inspect",
        "ragent-command-sandbox:v2",
        "--format",
        "{{.Id}}",
        NULL,
    };
    if (docker(socket, inspect, 5000, deadline, cancelled, &captured) != 0) {
        return failed(result, "命令运行镜像不可用，请先部署，未执行");
    }
    captured.output[strcspn(captured.output, "\r\n")] = '\0';
    char image[80];
    if (strlen(captured.output) != 71 || strncmp(captured.output, "sha256:", 7) != 0) {
        return failed(result, "命令运行镜像身份非法，未执行");
    }
    strcpy(image, captured.output);
    if (expected_image != NULL && strcmp(image, expected_image) != 0) {
        return failed(result, "命令运行镜像在确认后发生变化，批准失效，未执行");
    }
    char temporary[128];
    snprintf(
        temporary, sizeof(temporary), "/run/user/%lu/ragent-mask-XXXXXX", (unsigned long)getuid());
    if (mkdtemp(temporary) == NULL) {
        return failed(result, "无法创建受保护目录，未执行");
    }
    char blocked[4096];
    char empty[4096];
    if (snprintf(blocked, sizeof(blocked), "%s/blocked", temporary) >= (int)sizeof(blocked)
        || snprintf(empty, sizeof(empty), "%s/empty", temporary) >= (int)sizeof(empty)) {
        rmdir(temporary);
        return failed(result, "保护目录过长，未执行");
    }
    int hidden = open(blocked, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0000);
    if (hidden < 0 || mkdir(empty, 0000) != 0) {
        if (hidden >= 0) {
            close(hidden);
            unlink(blocked);
        }
        rmdir(temporary);
        return failed(result, "无法建立敏感文件遮蔽，未执行");
    }
    close(hidden);
    unsigned char random[16];
    if (getrandom(random, sizeof(random), 0) != sizeof(random)) {
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        return failed(result, "无法生成容器标识，未执行");
    }
    char name[80];
    strcpy(name, "ragent-command-");
    for (size_t index = 0; index < sizeof(random); ++index) {
        snprintf(name + 15 + index * 2, 3, "%02x", random[index]);
    }
    char reference[128];
    char actual[4096];
    char directory[4096];
    snprintf(reference, sizeof(reference), "/proc/self/fd/%d", workspace);
    ssize_t actual_length = readlink(reference, actual, sizeof(actual) - 1);
    if (actual_length < 0 || (size_t)actual_length >= sizeof(actual) - 1) {
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        return failed(result, "无法解析授权工作区，未执行");
    }
    actual[actual_length] = '\0';
    char *source = mount_spec(actual, "/workspace", false);
    if (source == NULL) {
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        return failed(result, "无法构造工作区挂载，未执行");
    }
    if (snprintf(directory, sizeof(directory), "/workspace/%s", cwd) >= (int)sizeof(directory)) {
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        free(source);
        return failed(result, "工作目录过长，未执行");
    }
    NetworkGateway gateway = { 0 };
    CommandAuthorization offline;
    authorization_init(&offline);
    if (authorization == NULL) {
        authorization = &offline;
    }
    Arguments create = { 0 };
    const char *fixed[] = {
        "create",
        "--name",
        name,
        "--label",
        "org.ragent.command=true",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
        "--tmpfs",
        "/root:rw,nosuid,nodev,size=16m,mode=0700",
        "--mount",
        source,
        NULL,
    };
    int status = 0;
    for (size_t index = 0; fixed[index] != NULL; ++index) {
        if (append(&create, fixed[index]) != 0) {
            status = -1;
            break;
        }
    }
    free(source);
    if (status == 0) {
        status = masks(&create, workspace, "", empty, blocked, &trusted, 0);
    }
    if (status != 0) {
        destroy_arguments(&create);
        authorization_destroy(&offline);
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        return failed(result, "无法建立完整工作区保护，未执行");
    }
    json_object *owner = owner_current();
    char owner_label[1024];
    if (owner == NULL || owner_save(temporary, owner) != 0) {
        if (owner != NULL) {
            json_object_put(owner);
        }
        destroy_arguments(&create);
        authorization_destroy(&offline);
        unlink(blocked);
        rmdir(empty);
        rmdir(temporary);
        return failed(result, "无法记录沙箱资源所有者，未执行");
    }
    snprintf(owner_label,
        sizeof(owner_label),
        "org.ragent.owner=%s",
        json_object_to_json_string_ext(owner, JSON_C_TO_STRING_PLAIN));
    json_object_put(owner);
    status = append(&create, "--label");
    if (status == 0) {
        status = append(&create, owner_label);
    }
    if (status == 0) {
        status = authorization_snapshot(authorization, temporary);
    }
    bool online = json_object_array_length(authorization->network) != 0;
    if (status == 0 && online) {
        status = network_start(socket,
            authorization->gateway_image,
            temporary,
            authorization->network,
            &gateway,
            deadline,
            cancelled);
    }
    const char *attachments[] = { "authorization.json", "credentials", "network.sock", NULL };
    for (size_t index = 0; status == 0 && attachments[index] != NULL; ++index) {
        if (index == 2 && !online) {
            continue;
        }
        char host_path[4096];
        char target_path[128];
        snprintf(host_path, sizeof(host_path), "%s/%s", temporary, attachments[index]);
        snprintf(target_path, sizeof(target_path), "/run/ragent/%s", attachments[index]);
        char *spec = mount_spec(host_path, target_path, true);
        if (spec == NULL) {
            status = -1;
        } else {
            status = append(&create, "--mount");
            if (status == 0) {
                status = append(&create, spec);
            }
            free(spec);
        }
    }
    const char *tail[] = {
        image,
        "/usr/bin/sleep",
        "infinity",
        NULL,
    };
    for (size_t index = 0; status == 0 && tail[index] != NULL; ++index) {
        status = append(&create, tail[index]);
    }
    if (status != 0) {
        destroy_arguments(&create);
        failed(result, "无法完成本次授权与隔离配置，未执行");
        goto cleanup;
    }
    status = docker(socket, create.values, 10000, deadline, cancelled, &captured);
    destroy_arguments(&create);
    if (status != 0) {
        failed(result, "容器创建失败，未执行");
        goto cleanup;
    }
    char *start[] = { "start", name, NULL };
    if (docker(socket, start, 10000, deadline, cancelled, &captured) != 0) {
        failed(result, "容器启动失败，未执行");
        status = -1;
        goto cleanup;
    }
    const char *probe = "import os,json,pathlib,socket\n"
                        "s=os.stat('/workspace');v=os.statvfs('/')\n"
                        "p=pathlib.Path('/sys/fs/cgroup')\n"
                        "state=pathlib.Path('/proc/self/status').read_text()\n"
                        "ok=os.getuid()==0 and bool(v.f_flag & os.ST_RDONLY)\n"
                        "ok=ok and 'CapEff:\\t0000000000000000' in state\n"
                        "ok=ok and 'NoNewPrivs:\\t1' in state and 'Seccomp:\\t2' in state\n"
                        "ok=ok and (p/'memory.max').read_text().strip()=='536870912'\n"
                        "ok=ok and (p/'pids.max').read_text().strip()=='128'\n"
                        "ok=ok and (p/'cpu.max').read_text().strip()=='100000 100000'\n"
                        "n=socket.socket();n.settimeout(1)\n"
                        "try:\n n.connect(('8.8.8.8',443));ok=False\n"
                        "except OSError as e:\n ok=ok and e.errno==101\n"
                        "finally:\n n.close()\n"
                        "print(json.dumps({'ok':bool(ok),'device':s.st_dev,'inode':s.st_ino}))\n";
    char *check[] = { "exec", name, "/usr/bin/python3", "-I", "-S", "-c", (char *)probe, NULL };
    if (docker(socket, check, 10000, deadline, cancelled, &captured) != 0) {
        failed(result, "隔离探针不可用，未执行");
        status = -1;
        goto cleanup;
    }
    json_object *verified
        = json_parse_object((const unsigned char *)captured.output, captured.output_length);
    bool matched = verified != NULL
        && json_object_get_boolean(json_object_object_get(verified, "ok"))
        && (uint64_t)json_object_get_int64(json_object_object_get(verified, "device"))
            == root.st_dev
        && (uint64_t)json_object_get_int64(json_object_object_get(verified, "inode"))
            == root.st_ino;
    if (verified != NULL) {
        json_object_put(verified);
    }
    if (!matched) {
        failed(result, "隔离探针或工作区身份不匹配，未执行");
        status = -1;
        goto cleanup;
    }
    char *execute[] = {
        "exec",
        "--workdir",
        directory,
        name,
        "/usr/local/libexec/ragent-launcher",
        (char *)command,
        NULL,
    };
    if (*cancelled || command_time_left(deadline, 1) == 0
        || (monitor != NULL && !monitor(context))) {
        failed(result, "任务已超时、取消或连接失效，用户命令未执行");
        status = -1;
        goto cleanup;
    }
    outcome->invoked = true;
    char endpoint[512];
    snprintf(endpoint, sizeof(endpoint), "unix://%s", socket);
    char *argv[32] = { "docker", "--host", endpoint };
    size_t argument_count = 3;
    for (size_t index = 0; execute[index] != NULL; ++index) {
        argv[argument_count++] = execute[index];
    }
    status = command_capture_monitored("/usr/bin/docker",
        argv,
        NULL,
        0,
        65536,
        command_time_left(deadline, authorization->timeout_ms),
        -1,
        monitor,
        context,
        cancelled,
        result);
cleanup: {
    volatile sig_atomic_t cleanup_cancelled = 0;
    char *remove[] = { "rm", "--force", name, NULL };
    if (docker(socket, remove, 5000, 0, &cleanup_cancelled, &captured) != 0) {
        char *check[] = { "container", "inspect", name, NULL };
        int inspected = docker(socket, check, 5000, 0, &cleanup_cancelled, &captured);
        outcome->cleanup_failed = inspected == 0 || strstr(captured.output, "No such") == NULL;
    }
}
    if (!network_stop(socket, temporary, &gateway)) {
        outcome->cleanup_failed = true;
    }
    authorization_destroy(&offline);
    // 资源清理未完成时保留所有者记录，客户端退出后由独立回收服务继续清理。
    if (outcome->cleanup_failed) {
        return -1;
    }
    bool files_removed = authorization_cleanup(temporary);
    if (unlink(blocked) != 0) {
        files_removed = false;
    }
    if (rmdir(empty) != 0) {
        files_removed = false;
    }
    char metadata[4096];
    snprintf(metadata, sizeof(metadata), "%s/owner.json", temporary);
    if (!files_removed || unlink(metadata) != 0 || rmdir(temporary) != 0) {
        outcome->cleanup_failed = true;
        return -1;
    }
    return status;
}
