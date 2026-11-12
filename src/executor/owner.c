#define _GNU_SOURCE
#include "executor/owner.h"
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static bool read_identity(pid_t pid, char start[32], char boot[64])
{
    char path[128];
    snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
    struct stat info;
    if (stat(path, &info) != 0 || info.st_uid != getuid()) {
        return false;
    }
    FILE *file = fopen(path, "re");
    char line[4096];
    if (file == NULL) {
        return false;
    }
    char *read = fgets(line, sizeof(line), file);
    fclose(file);
    char *end = read == NULL ? NULL : strrchr(line, ')');
    if (end == NULL) {
        return false;
    }
    char *context = NULL;
    char *field = strtok_r(end + 2, " ", &context);
    if (field == NULL || *field == 'Z' || *field == 'X') {
        return false;
    }
    for (int index = 3; field != NULL && index < 22; ++index) {
        field = strtok_r(NULL, " ", &context);
    }
    if (field == NULL || strlen(field) >= 32) {
        return false;
    }
    strcpy(start, field);
    file = fopen("/proc/sys/kernel/random/boot_id", "re");
    if (file == NULL) {
        return false;
    }
    read = fgets(boot, 64, file);
    fclose(file);
    if (read == NULL) {
        return false;
    }
    boot[strcspn(boot, "\r\n")] = '\0';
    return true;
}

json_object *owner_current(void)
{
    char start[32];
    char boot[64];
    if (!read_identity(getpid(), start, boot)) {
        return NULL;
    }
    json_object *identity = json_object_new_object();
    json_object_object_add(identity, "pid", json_object_new_int64(getpid()));
    json_object_object_add(identity, "start", json_object_new_string(start));
    json_object_object_add(identity, "boot", json_object_new_string(boot));
    return identity;
}

bool owner_alive(json_object *identity)
{
    if (!json_object_is_type(identity, json_type_object)) {
        return false;
    }
    json_object *pid = json_object_object_get(identity, "pid");
    json_object *expected_start = json_object_object_get(identity, "start");
    json_object *expected_boot = json_object_object_get(identity, "boot");
    if (!json_object_is_type(pid, json_type_int) || json_object_get_int64(pid) < 1
        || json_object_get_int64(pid) > INT_MAX
        || !json_object_is_type(expected_start, json_type_string)
        || !json_object_is_type(expected_boot, json_type_string)) {
        return false;
    }
    char start[32];
    char boot[64];
    return read_identity((pid_t)json_object_get_int64(pid), start, boot)
        && strcmp(start, json_object_get_string(expected_start)) == 0
        && strcmp(boot, json_object_get_string(expected_boot)) == 0;
}

int owner_save(const char *directory, json_object *identity)
{
    int root = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) {
        return -1;
    }
    int file = openat(root, "owner.json.tmp", O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (file < 0) {
        close(root);
        return -1;
    }
    const char *text = json_object_to_json_string_ext(identity, JSON_C_TO_STRING_PLAIN);
    bool written = dprintf(file, "%s\n", text) >= 0;
    int status = close(file);
    if (written && status == 0) {
        status = renameat(root, "owner.json.tmp", root, "owner.json");
    } else {
        status = -1;
    }
    if (status != 0) {
        unlinkat(root, "owner.json.tmp", 0);
    }
    close(root);
    return status;
}
