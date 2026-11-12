#define _GNU_SOURCE
#include "executor/workspace.h"
#include "protocol/protocol.h"
#include <fcntl.h>
#include <errno.h>
#include <stdbool.h>
#include <sys/socket.h>
#include <sys/statvfs.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bool contains(const char *path, const char *expected)
{
    FILE *file = fopen(path, "re");
    if (file == NULL) {
        return false;
    }
    char line[4096];
    bool found = false;
    while (fgets(line, sizeof(line), file) != NULL) {
        if (strstr(line, expected) != NULL) {
            found = true;
            break;
        }
    }
    fclose(file);
    return found;
}

static int probe(void)
{
    struct statvfs root = {0};
    struct statvfs workspace = {0};
    bool ok = getuid() == 1000 && getgid() == 1000 &&
        statvfs("/", &root) == 0 && (root.f_flag & ST_RDONLY) != 0 &&
        statvfs("/workspace", &workspace) == 0 &&
        (workspace.f_flag & (ST_NOEXEC | ST_NOSUID | ST_NODEV)) ==
            (ST_NOEXEC | ST_NOSUID | ST_NODEV) &&
        workspace.f_blocks * workspace.f_frsize == 16 * 1024 * 1024 &&
        contains("/proc/self/status", "CapEff:\t0000000000000000") &&
        contains("/proc/self/status", "NoNewPrivs:\t1") &&
        contains("/proc/self/status", "Seccomp:\t2") &&
        contains("/sys/fs/cgroup/memory.max", "134217728") &&
        contains("/sys/fs/cgroup/pids.max", "32") &&
        contains("/sys/fs/cgroup/cpu.max", "50000 100000");
    int fd = open("/workspace/.ragent-probe", O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) {
        ok = false;
    } else {
        bool written = write(fd, "probe", 5) == 5;
        bool closed = close(fd) == 0;
        bool removed = unlink("/workspace/.ragent-probe") == 0;
        ok = ok && written && closed && removed;
    }
    int network = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    struct sockaddr_in remote = {
        .sin_family = AF_INET,
        .sin_port = htons(443),
        .sin_addr = {.s_addr = htonl(0x08080808)},
    };
    if (network < 0) {
        ok = false;
    } else {
        if (connect(network, (struct sockaddr *)&remote, sizeof(remote)) == 0 ||
            errno != ENETUNREACH) {
            ok = false;
        }
        close(network);
    }
    if (!ok) {
        fprintf(stderr, "隔离探针: uid=%u root=%lu workspace=%lu bytes=%lu "
                "caps=%d nnp=%d seccomp=%d memory=%d pids=%d cpu=%d\n",
                getuid(), root.f_flag, workspace.f_flag,
                workspace.f_blocks * workspace.f_frsize,
                contains("/proc/self/status", "CapEff:\t0000000000000000"),
                contains("/proc/self/status", "NoNewPrivs:\t1"),
                contains("/proc/self/status", "Seccomp:\t2"),
                contains("/sys/fs/cgroup/memory.max", "134217728"),
                contains("/sys/fs/cgroup/pids.max", "32"),
                contains("/sys/fs/cgroup/cpu.max", "50000 100000"));
        return 1;
    }
    puts("{\"ok\":true,\"code\":\"\",\"output\":\"隔离探针通过\","
         "\"effect\":\"none\",\"exit_code\":0,\"signal\":0}");
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--probe") == 0) {
        return probe();
    }
    if (argc != 1) {
        return 1;
    }
    size_t capacity = 131072;
    unsigned char *input = malloc(capacity);
    if (input == NULL) {
        return 1;
    }
    size_t length = fread(input, 1, capacity, stdin);
    json_object *request = length < capacity ? json_parse_object(input, length) : NULL;
    free(input);
    int root = open("/workspace", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (root < 0 || request == NULL) {
        if (root >= 0) {
            close(root);
        }
        if (request != NULL) {
            json_object_put(request);
        }
        return 1;
    }
    json_object *result = workspace_execute(root, request);
    close(root);
    json_object_put(request);
    if (result == NULL) {
        return 1;
    }
    const char *text = json_object_to_json_string_ext(result, JSON_C_TO_STRING_PLAIN);
    int status = puts(text) < 0 || fflush(stdout) != 0 ? 1 : 0;
    json_object_put(result);
    return status;
}
