#define _GNU_SOURCE
#include "executor/observe.h"
#include "executor/policy.h"
#include "protocol/protocol.h"
#include <stdio.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <sys/syscall.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    unsigned char input[131072];
    size_t length = fread(input, 1, sizeof(input), stdin);
    json_object *request = length < sizeof(input) ? json_parse_object(input, length) : NULL;
    json_object *command = NULL;
    json_object *workspace = NULL;
    if (request == NULL || !json_object_object_get_ex(request, "command", &command)
        || !json_object_is_type(command, json_type_string)
        || !json_object_object_get_ex(request, "workspace", &workspace)
        || !json_object_is_type(workspace, json_type_string)
        || strlen(json_object_get_string(command)) != (size_t)json_object_get_string_len(command)
        || strlen(json_object_get_string(workspace))
            != (size_t)json_object_get_string_len(workspace)) {
        if (request != NULL) {
            json_object_put(request);
        }
        return 126;
    }
    int status = 126;
    int root = -1;
    if (argc == 2 && strcmp(argv[1], "--workspace-fd") == 0) {
        root = 3;
        if (fcntl(root, F_SETFD, FD_CLOEXEC) != 0) {
            json_object_put(request);
            return 126;
        }
    } else if (argc == 1) {
        root = open(json_object_get_string(workspace), O_PATH | O_DIRECTORY | O_CLOEXEC);
    }
    json_object *cwd_field = json_object_object_get(request, "cwd");
    const char *cwd = ".";
    if (cwd_field != NULL) {
        if (!json_object_is_type(cwd_field, json_type_string)
            || strlen(json_object_get_string(cwd_field))
                != (size_t)json_object_get_string_len(cwd_field)) {
            json_object_put(request);
            if (root >= 0) {
                close(root);
            }
            return 126;
        }
        cwd = json_object_get_string(cwd_field);
    }
    struct open_how how = {
        .flags = O_PATH | O_DIRECTORY | O_CLOEXEC,
        .resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS,
    };
    int directory = root < 0 ? -1 : (int)syscall(SYS_openat2, root, cwd, &how, sizeof(how));
    if (directory >= 0 && fchdir(directory) == 0) {
        status = observe_command(json_object_get_string(command), root, cwd);
    } else {
        perror("工作区不可用");
    }
    if (directory >= 0) {
        close(directory);
    }
    if (root >= 0) {
        close(root);
    }
    json_object_put(request);
    return status;
}
