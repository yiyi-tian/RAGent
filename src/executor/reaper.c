#define _GNU_SOURCE
#include "executor/command.h"
#include "executor/owner.h"
#include "protocol/protocol.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;
static char endpoint[256];

static void stop(int number)
{
    (void)number;
    stopping = 1;
}

static int docker(char *const command[], CommandResult *result)
{
    char *argv[32] = { "docker", "--host", endpoint };
    size_t count = 3;
    for (size_t index = 0; command[index] != NULL; ++index) {
        if (count == 31) {
            return -1;
        }
        argv[count++] = command[index];
    }
    return command_capture("/usr/bin/docker", argv, NULL, 0, 65536, 5000, &stopping, result);
}

static const char *text_field(json_object *object, const char *name)
{
    if (!json_object_is_type(object, json_type_object)) {
        return NULL;
    }
    json_object *value = json_object_object_get(object, name);
    if (!json_object_is_type(value, json_type_string)) {
        return NULL;
    }
    const char *text = json_object_get_string(value);
    return strlen(text) == (size_t)json_object_get_string_len(value) ? text : NULL;
}

static bool managed_name(const char *name)
{
    const char *prefix = "ragent-command-";
    if (strncmp(name, prefix, strlen(prefix)) != 0 || strlen(name) != strlen(prefix) + 32) {
        return false;
    }
    for (const char *byte = name + strlen(prefix); *byte != '\0'; ++byte) {
        if (strchr("0123456789abcdef", *byte) == NULL) {
            return false;
        }
    }
    return true;
}

static int reap_containers(void)
{
    CommandResult list;
    char *args[] = {
        "ps",
        "--all",
        "--filter",
        "label=org.ragent.command=true",
        "--format",
        "{{.Names}}",
        NULL,
    };
    if (docker(args, &list) != 0) {
        return -1;
    }
    char *context = NULL;
    for (char *name = strtok_r(list.output, "\n", &context); name != NULL;
         name = strtok_r(NULL, "\n", &context)) {
        if (!managed_name(name)) {
            continue;
        }
        char *inspect[] = { "inspect", name, "--format", "{{json .Config.Labels}}", NULL };
        CommandResult captured;
        if (docker(inspect, &captured) != 0) {
            continue;
        }
        json_object *labels
            = json_parse_object((const unsigned char *)captured.output, captured.output_length);
        const char *encoded = text_field(labels, "org.ragent.owner");
        json_object *owner = encoded == NULL
            ? NULL
            : json_parse_object((const unsigned char *)encoded, strlen(encoded));
        bool dead = owner != NULL && !owner_alive(owner);
        if (owner != NULL) {
            json_object_put(owner);
        }
        if (labels != NULL) {
            json_object_put(labels);
        }
        if (dead) {
            char *remove[] = { "rm", "--force", name, NULL };
            if (docker(remove, &captured) != 0) {
                return -1;
            }
        }
    }
    return 0;
}

static int remove_entries(int directory, dev_t device, unsigned depth)
{
    if (depth > 4) {
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
        struct stat info;
        if (fstatat(directory, entry->d_name, &info, AT_SYMLINK_NOFOLLOW) != 0
            || info.st_uid != getuid() || info.st_dev != device) {
            status = -1;
            break;
        }
        if (S_ISDIR(info.st_mode)) {
            if (unlinkat(directory, entry->d_name, AT_REMOVEDIR) == 0) {
                continue;
            }
            int child
                = openat(directory, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) {
                status = -1;
                break;
            }
            status = remove_entries(child, device, depth + 1);
            close(child);
            if (status != 0 || unlinkat(directory, entry->d_name, AT_REMOVEDIR) != 0) {
                status = -1;
                break;
            }
        } else if (unlinkat(directory, entry->d_name, 0) != 0) {
            status = -1;
            break;
        }
    }
    closedir(stream);
    return status;
}

static int reap_runtime(void)
{
    char path[128];
    snprintf(path, sizeof(path), "/run/user/%lu", (unsigned long)getuid());
    DIR *stream = opendir(path);
    if (stream == NULL) {
        return -1;
    }
    int root = dirfd(stream);
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
        if (strncmp(entry->d_name, "ragent-mask-", 12) != 0 || strlen(entry->d_name) != 18) {
            continue;
        }
        int job = openat(root, entry->d_name, O_DIRECTORY | O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
        struct stat info;
        if (job < 0 || fstat(job, &info) != 0 || info.st_uid != getuid()
            || (info.st_mode & 0077) != 0) {
            if (job >= 0) {
                close(job);
            }
            continue;
        }
        int metadata = openat(job, "owner.json", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
        unsigned char bytes[1024];
        ssize_t length = metadata < 0 ? -1 : read(metadata, bytes, sizeof(bytes));
        if (metadata >= 0) {
            close(metadata);
        }
        json_object *owner = length > 0 ? json_parse_object(bytes, (size_t)length) : NULL;
        if (owner == NULL || owner_alive(owner)) {
            if (owner != NULL) {
                json_object_put(owner);
            }
            close(job);
            continue;
        }
        json_object_put(owner);
        if (remove_entries(job, info.st_dev, 0) != 0
            || unlinkat(root, entry->d_name, AT_REMOVEDIR) != 0) {
            status = -1;
        }
        close(job);
    }
    closedir(stream);
    return status;
}

int main(int argc, char **argv)
{
    if (getuid() == 0 || argc > 2 || (argc == 2 && strcmp(argv[1], "--once") != 0)) {
        return 2;
    }
    snprintf(endpoint,
        sizeof(endpoint),
        "unix:///run/user/%lu/ragent-sandbox-docker.sock",
        (unsigned long)getuid());
    struct sigaction action = { 0 };
    action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0 || sigaction(SIGINT, &action, NULL) != 0
        || signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        return 1;
    }
    do {
        int status = reap_containers();
        if (status == 0) {
            status = reap_runtime();
        }
        if (argc == 2) {
            return status == 0 ? 0 : 1;
        }
        if (status != 0 && !stopping) {
            fputs("[WARN] 命令资源回收未完成，将继续检查\n", stderr);
        }
        struct timespec interval = {
            .tv_sec = 2,
            .tv_nsec = 0,
        };
        if (!stopping) {
            nanosleep(&interval, NULL);
        }
    } while (!stopping);
    return 0;
}
