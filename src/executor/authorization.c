#define _GNU_SOURCE
#include "executor/authorization.h"
#include "executor/network.h"
#include "executor/command.h"
#include "client/terminal.h"
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <termios.h>
#include <unistd.h>

void authorization_init(CommandAuthorization *authorization)
{
    memset(authorization, 0, sizeof(*authorization));
    authorization->network = json_object_new_array();
    authorization->environment = json_object_new_object();
    authorization->files = json_object_new_array();
    authorization->timeout_ms = 120000;
}

void authorization_destroy(CommandAuthorization *authorization)
{
    for (size_t index = 0; index < json_object_array_length(authorization->files); ++index) {
        json_object *file = json_object_array_get_idx(authorization->files, index);
        close(json_object_get_int(json_object_object_get(file, "fd")));
    }
    json_object_put(authorization->network);
    json_object_put(authorization->environment);
    json_object_put(authorization->files);
    memset(authorization, 0, sizeof(*authorization));
}

static bool line_input(
    const char *prompt, char *line, size_t capacity, bool secret, int64_t deadline)
{
    struct termios original;
    if (secret) {
        if (tcgetattr(STDIN_FILENO, &original) != 0) {
            return false;
        }
        struct termios hidden = original;
        hidden.c_lflag &= ~(ECHO | ECHONL);
        if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &hidden) != 0) {
            return false;
        }
    }
    fputs(prompt, stderr);
    if (fflush(stderr) != 0) {
        if (secret) {
            tcsetattr(STDIN_FILENO, TCSAFLUSH, &original);
        }
        return false;
    }
    struct pollfd input = {
        .fd = STDIN_FILENO,
        .events = POLLIN,
    };
    bool ok = poll(&input, 1, (int)command_time_left(deadline, 900000)) == 1
        && fgets(line, (int)capacity, stdin) != NULL;
    if (secret) {
        bool restored = tcsetattr(STDIN_FILENO, TCSAFLUSH, &original) == 0;
        fputc('\n', stderr);
        ok = ok && restored;
    }
    if (!ok) {
        return false;
    }
    if (strchr(line, '\n') == NULL) {
        int byte;
        do {
            byte = getchar();
        } while (byte != '\n' && byte != EOF);
        return false;
    }
    line[strcspn(line, "\r\n")] = '\0';
    return true;
}

static bool environment_name(const char *name)
{
    if (strlen(name) == 0 || strlen(name) > 128 || !(name[0] >= 'A' && name[0] <= 'Z')) {
        return false;
    }
    for (const char *byte = name; *byte != '\0'; ++byte) {
        if (!((*byte >= 'A' && *byte <= 'Z') || (*byte >= '0' && *byte <= '9') || *byte == '_')) {
            return false;
        }
    }
    const char *blocked[] = {
        "PATH",
        "HOME",
        "SHELL",
        "ENV",
        "BASH_ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "IFS",
        "GLOBIGNORE",
        "SSH_AUTH_SOCK",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "DISPLAY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        NULL,
    };
    for (size_t index = 0; blocked[index] != NULL; ++index) {
        if (strcmp(name, blocked[index]) == 0) {
            return false;
        }
    }
    return strncmp(name, "LD_", 3) != 0 && strncmp(name, "PYTHON", 6) != 0
        && strncmp(name, "BASH_FUNC_", 10) != 0;
}

bool authorization_timeout(CommandAuthorization *authorization)
{
    authorization->approval_deadline_ms = command_now_ms() + 900000;
    char line[64];
    if (!line_input("执行时限秒（1至600，空行使用120）：",
            line,
            sizeof(line),
            false,
            authorization->approval_deadline_ms)) {
        return false;
    }
    if (line[0] == '\0') {
        return true;
    }
    char *end;
    errno = 0;
    unsigned long seconds = strtoul(line, &end, 10);
    if (errno != 0 || *end != '\0' || seconds == 0 || seconds > 600) {
        return false;
    }
    authorization->timeout_ms = (unsigned)seconds * 1000;
    return true;
}

bool authorization_prompt(CommandAuthorization *authorization)
{
    char line[4096];
    fputs("网络按完整域名和端口授权，每行一项，如 https://example.com:443 或 "
          "ssh://git.example.com:22；空行结束，默认无网络。\n",
        stderr);
    for (unsigned index = 0; index < 32; ++index) {
        if (!line_input(
                "允许的地址：", line, sizeof(line), false, authorization->approval_deadline_ms)) {
            return false;
        }
        if (line[0] == '\0') {
            break;
        }
        if (!network_grant_valid(line)) {
            fputs("地址非法；不接受IP、通配符、路径和省略端口。本次已取消。\n", stderr);
            return false;
        }
        json_object_array_add(authorization->network, json_object_new_string(line));
    }
    fputs("可单独提供本次命令的环境凭证，值不回传模型；空行跳过。\n", stderr);
    for (unsigned index = 0; index < 16; ++index) {
        if (!line_input("凭证环境变量名：",
                line,
                sizeof(line),
                false,
                authorization->approval_deadline_ms)) {
            return false;
        }
        if (line[0] == '\0') {
            break;
        }
        if (!environment_name(line)) {
            fputs("凭证名称非法或属于执行器控制变量，本次已取消。\n", stderr);
            return false;
        }
        char value[8192];
        if (!line_input("凭证值（隐藏输入）：",
                value,
                sizeof(value),
                true,
                authorization->approval_deadline_ms)) {
            return false;
        }
        json_object_object_add(authorization->environment, line, json_object_new_string(value));
        explicit_bzero(value, sizeof(value));
    }
    fputs("可单独授权本机凭证文件；只读快照放入容器 /run/ragent/credentials/文件名。\n", stderr);
    for (unsigned index = 0; index < 16; ++index) {
        if (!line_input("本机凭证文件绝对路径（空行结束）：",
                line,
                sizeof(line),
                false,
                authorization->approval_deadline_ms)) {
            return false;
        }
        if (line[0] == '\0') {
            break;
        }
        int fd = line[0] == '/' ? open(line, O_RDONLY | O_NOFOLLOW | O_CLOEXEC) : -1;
        struct stat info;
        if (fd < 0 || fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid()
            || info.st_size < 0 || info.st_size > 1048576) {
            if (fd >= 0) {
                close(fd);
            }
            fputs("文件必须是当前用户拥有、至多1MiB的普通文件，本次已取消。\n", stderr);
            return false;
        }
        json_object *file = json_object_new_object();
        json_object_object_add(file, "source", json_object_new_string(line));
        json_object_object_add(file, "fd", json_object_new_int(fd));
        json_object_array_add(authorization->files, file);
        if (!line_input("容器内文件名（仅字母、数字、下划线和短横线）：",
                line,
                sizeof(line),
                false,
                authorization->approval_deadline_ms)) {
            return false;
        }
        if (strlen(line) == 0 || strlen(line) > 100) {
            return false;
        }
        for (const char *byte = line; *byte != '\0'; ++byte) {
            if (!isalnum((unsigned char)*byte) || (unsigned char)*byte > 127) {
                if (*byte != '_' && *byte != '-') {
                    return false;
                }
            }
        }
        for (size_t previous = 0; previous + 1 < json_object_array_length(authorization->files);
             ++previous) {
            json_object *item = json_object_array_get_idx(authorization->files, previous);
            if (strcmp(json_object_get_string(json_object_object_get(item, "name")), line) == 0) {
                return false;
            }
        }
        json_object_object_add(file, "name", json_object_new_string(line));
    }
    return true;
}

bool authorization_has_credentials(const CommandAuthorization *authorization)
{
    return json_object_object_length(authorization->environment) != 0
        || json_object_array_length(authorization->files) != 0;
}

static int write_all(int fd, const void *bytes, size_t length)
{
    const unsigned char *input = bytes;
    while (length > 0) {
        ssize_t count = write(fd, input, length);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return -1;
        }
        input += count;
        length -= (size_t)count;
    }
    return 0;
}

int authorization_snapshot(const CommandAuthorization *authorization, const char *runtime)
{
    char path[4096];
    snprintf(path, sizeof(path), "%s/credentials", runtime);
    if (mkdir(path, 0700) != 0) {
        return -1;
    }
    for (size_t index = 0; index < json_object_array_length(authorization->files); ++index) {
        json_object *file = json_object_array_get_idx(authorization->files, index);
        const char *name = json_object_get_string(json_object_object_get(file, "name"));
        int source = json_object_get_int(json_object_object_get(file, "fd"));
        snprintf(path, sizeof(path), "%s/credentials/%s", runtime, name);
        int target = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0400);
        if (target < 0 || lseek(source, 0, SEEK_SET) < 0) {
            if (target >= 0) {
                close(target);
            }
            return -1;
        }
        unsigned char bytes[8192];
        size_t total = 0;
        bool ok = true;
        for (;;) {
            ssize_t length = read(source, bytes, sizeof(bytes));
            if (length < 0 && errno == EINTR) {
                continue;
            }
            if (length == 0) {
                break;
            }
            if (length < 0 || total + (size_t)length > 1048576
                || write_all(target, bytes, (size_t)length) != 0) {
                ok = false;
                break;
            }
            total += (size_t)length;
        }
        explicit_bzero(bytes, sizeof(bytes));
        if (close(target) != 0 || !ok) {
            return -1;
        }
    }
    snprintf(path, sizeof(path), "%s/authorization.json", runtime);
    int fd = open(path, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0400);
    if (fd < 0) {
        return -1;
    }
    json_object *config = json_object_new_object();
    json_object_object_add(config, "environment", json_object_get(authorization->environment));
    json_object_object_add(config,
        "network",
        json_object_new_boolean(json_object_array_length(authorization->network) != 0));
    const char *encoded = json_object_to_json_string_ext(config, JSON_C_TO_STRING_PLAIN);
    int status = write_all(fd, encoded, strlen(encoded));
    json_object_put(config);
    if (close(fd) != 0) {
        status = -1;
    }
    return status;
}

bool authorization_cleanup(const char *runtime)
{
    char path[4096];
    snprintf(path, sizeof(path), "%s/credentials", runtime);
    DIR *directory = opendir(path);
    bool ok = true;
    if (directory != NULL) {
        struct dirent *entry;
        while ((entry = readdir(directory)) != NULL) {
            if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
                continue;
            }
            if (unlinkat(dirfd(directory), entry->d_name, 0) != 0) {
                ok = false;
            }
        }
        closedir(directory);
        if (rmdir(path) != 0) {
            ok = false;
        }
    } else if (errno != ENOENT) {
        ok = false;
    }
    snprintf(path, sizeof(path), "%s/authorization.json", runtime);
    if (unlink(path) != 0 && errno != ENOENT) {
        ok = false;
    }
    return ok;
}
