#define _GNU_SOURCE
#include "executor/workspace.h"
#include "protocol/protocol.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/openat2.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define FILE_LIMIT 16384U
#define ENTRY_LIMIT 128U

static json_object *reply(const char *code, const char *message, const char *effect)
{
    json_object *result = json_object_new_object();
    if (result == NULL) {
        return NULL;
    }
    json_object_object_add(result, "ok", json_object_new_boolean(code == NULL));
    json_object_object_add(result, "code", json_object_new_string(code == NULL ? "" : code));
    json_object_object_add(result, "message", json_object_new_string(message));
    json_object_object_add(result, "output", json_object_new_string(message));
    json_object_object_add(result, "effect", json_object_new_string(effect));
    json_object_object_add(result, "exit_code", json_object_new_int(code == NULL ? 0 : -1));
    json_object_object_add(result, "signal", json_object_new_int(0));
    return result;
}

static const char *string_field(json_object *object, const char *name)
{
    json_object *value = NULL;
    if (!json_object_object_get_ex(object, name, &value) ||
        !json_object_is_type(value, json_type_string)) {
        return NULL;
    }
    const char *text = json_object_get_string(value);
    return strlen(text) == (size_t)json_object_get_string_len(value) ? text : NULL;
}

static bool valid_path(const char *path)
{
    if (path == NULL || path[0] == '/' || path[0] == '\0' || strlen(path) > 512) {
        return false;
    }
    const char *segment = path;
    size_t depth = 0;
    for (const char *p = path; ; ++p) {
        if (((unsigned char)*p < 32 && *p != '\0') || (unsigned char)*p == 127) {
            return false;
        }
        if (*p == '/' || *p == '\0') {
            size_t length = (size_t)(p - segment);
            if (length == 0 || length > 128 || ++depth > 16 ||
                (length == 2 && strncmp(segment, "..", 2) == 0) ||
                (length == 1 && *segment == '.' && !(segment == path && *p == '\0')) ||
                (length >= 8 && strncmp(segment, ".ragent-", 8) == 0)) {
                return false;
            }
            if (*p == '\0') {
                break;
            }
            segment = p + 1;
        }
    }
    return true;
}

static int beneath(int root, const char *path, int flags)
{
    struct open_how how = {
        .flags = (unsigned long long)(flags | O_CLOEXEC),
        .resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
    };
    return (int)syscall(SYS_openat2, root, path, &how, sizeof(how));
}

static int parent_fd(int root, const char *path, char leaf[129])
{
    if (!valid_path(path) || strcmp(path, ".") == 0) {
        errno = EPERM;
        return -1;
    }
    char parent[513];
    strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (slash == NULL) {
        strcpy(leaf, path);
        strcpy(parent, ".");
    } else {
        strcpy(leaf, slash + 1);
        *slash = '\0';
    }
    return beneath(root, parent, O_RDONLY | O_DIRECTORY);
}

static int count_entries(int directory, unsigned *count)
{
    int fd = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    DIR *stream = fdopendir(fd);
    if (stream == NULL) {
        close(fd);
        return -1;
    }
    int status = 0;
    struct dirent *entry;
    errno = 0;
    while ((entry = readdir(stream)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        struct stat info;
        if (fstatat(directory, entry->d_name, &info, AT_SYMLINK_NOFOLLOW) != 0 ||
            S_ISLNK(info.st_mode) || ++*count > ENTRY_LIMIT) {
            status = -1;
            break;
        }
        if (S_ISDIR(info.st_mode)) {
            int child = beneath(directory, entry->d_name, O_RDONLY | O_DIRECTORY);
            if (child < 0) {
                status = -1;
                break;
            }
            status = count_entries(child, count);
            close(child);
            if (status != 0) {
                break;
            }
        }
        errno = 0;
    }
    if (entry == NULL && errno != 0) {
        status = -1;
    }
    closedir(stream);
    return status;
}

static int read_text(int root, const char *path, char data[FILE_LIMIT + 1], size_t *length)
{
    int fd = beneath(root, path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        return -1;
    }
    struct stat info;
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_nlink != 1 ||
        info.st_size > FILE_LIMIT) {
        close(fd);
        errno = EFBIG;
        return -1;
    }
    *length = 0;
    while (*length <= FILE_LIMIT) {
        ssize_t n = read(fd, data + *length, FILE_LIMIT + 1 - *length);
        if (n == 0) {
            break;
        }
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(fd);
            return -1;
        }
        *length += (size_t)n;
    }
    close(fd);
    if (*length > FILE_LIMIT || memchr(data, '\0', *length) != NULL) {
        errno = EFBIG;
        return -1;
    }
    data[*length] = '\0';
    json_object *test = json_object_new_object();
    json_object_object_add(test, "text", json_object_new_string_len(data, (int)*length));
    const char *encoded = json_object_to_json_string_ext(test, JSON_C_TO_STRING_PLAIN);
    json_object *parsed = json_parse_object((const unsigned char *)encoded, strlen(encoded));
    json_object_put(test);
    if (parsed == NULL) {
        errno = EILSEQ;
        return -1;
    }
    json_object_put(parsed);
    return 0;
}

static int write_text(int root, const char *path, const char *data, bool overwrite)
{
    char leaf[129];
    int parent = parent_fd(root, path, leaf);
    if (parent < 0) {
        return -1;
    }
    struct stat info;
    int found = fstatat(parent, leaf, &info, AT_SYMLINK_NOFOLLOW);
    if ((found == 0 && (!overwrite || !S_ISREG(info.st_mode) || info.st_nlink != 1)) ||
        (found != 0 && errno != ENOENT)) {
        close(parent);
        errno = EEXIST;
        return -1;
    }
    unsigned count = 0;
    if (count_entries(root, &count) != 0 || (found != 0 && count >= ENTRY_LIMIT)) {
        close(parent);
        errno = EDQUOT;
        return -1;
    }
    char temporary[64];
    snprintf(temporary, sizeof(temporary), ".ragent-%ld", (long)getpid());
    int fd = openat(parent, temporary, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) {
        close(parent);
        return -1;
    }
    size_t sent = 0;
    size_t length = strlen(data);
    int status = 0;
    while (sent < length) {
        ssize_t n = write(fd, data + sent, length - sent);
        if (n < 0 && errno == EINTR) {
            continue;
        }
        if (n <= 0) {
            status = -1;
            break;
        }
        sent += (size_t)n;
    }
    if (close(fd) != 0) {
        status = -1;
    }
    if (status == 0) {
        unsigned flags = overwrite ? 0 : RENAME_NOREPLACE;
        status = (int)syscall(SYS_renameat2, parent, temporary, parent, leaf, flags);
    }
    if (status != 0) {
        int saved = errno;
        if (unlinkat(parent, temporary, 0) != 0) {
            saved = errno;
        }
        errno = saved;
    }
    close(parent);
    return status;
}

static int compare_names(const void *left, const void *right)
{
    return strcmp(left, right);
}

static json_object *list_directory(int root, const char *path, json_object *args)
{
    json_object *offset_value = NULL;
    int offset = 0;
    if (json_object_object_get_ex(args, "offset", &offset_value)) {
        if (!json_object_is_type(offset_value, json_type_int) ||
            json_object_get_int64(offset_value) < 0 ||
            json_object_get_int64(offset_value) > ENTRY_LIMIT) {
            return reply("COMMAND_NOT_ALLOWED", "分页偏移非法", "none");
        }
        offset = json_object_get_int(offset_value);
    }
    int fd = beneath(root, path, O_RDONLY | O_DIRECTORY);
    if (fd < 0) {
        return reply("COMMAND_NOT_ALLOWED", "目录不存在或路径不允许", "none");
    }
    DIR *stream = fdopendir(fd);
    if (stream == NULL) {
        close(fd);
        return reply("COMMAND_FAILED", "无法读取目录", "none");
    }
    char names[ENTRY_LIMIT][129];
    size_t count = 0;
    struct dirent *entry;
    errno = 0;
    while ((entry = readdir(stream)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0 ||
            strncmp(entry->d_name, ".ragent-", 8) == 0) {
            continue;
        }
        if (count == ENTRY_LIMIT || !valid_path(entry->d_name)) {
            closedir(stream);
            return reply("COMMAND_OUTPUT_LIMIT", "目录包含过多或非法条目", "none");
        }
        strcpy(names[count++], entry->d_name);
    }
    if (errno != 0) {
        closedir(stream);
        return reply("COMMAND_FAILED", "读取目录失败", "none");
    }
    qsort(names, count, sizeof(names[0]), compare_names);
    json_object *page = json_object_new_object();
    json_object *items = json_object_new_array();
    size_t end = (size_t)offset + 32 < count ? (size_t)offset + 32 : count;
    for (size_t i = (size_t)offset; i < end; ++i) {
        struct stat info;
        if (fstatat(fd, names[i], &info, AT_SYMLINK_NOFOLLOW) != 0 || S_ISLNK(info.st_mode)) {
            json_object_put(page);
            json_object_put(items);
            closedir(stream);
            return reply("COMMAND_NOT_ALLOWED", "目录包含不允许的链接", "none");
        }
        json_object *item = json_object_new_object();
        json_object_object_add(item, "name", json_object_new_string(names[i]));
        const char *type = S_ISDIR(info.st_mode) ? "directory" : "file";
        json_object_object_add(item, "type", json_object_new_string(type));
        json_object_object_add(item, "size", json_object_new_int64(info.st_size));
        json_object_array_add(items, item);
    }
    closedir(stream);
    json_object_object_add(page, "entries", items);
    json_object_object_add(page, "next_offset", end < count ? json_object_new_int64(end) : NULL);
    json_object *result = reply(NULL,
        json_object_to_json_string_ext(page, JSON_C_TO_STRING_PLAIN), "none");
    json_object_put(page);
    return result;
}

json_object *workspace_execute(int root, json_object *request)
{
    const char *tool = string_field(request, "tool");
    json_object *args = NULL;
    if (tool == NULL || !json_object_object_get_ex(request, "args", &args) ||
        !json_object_is_type(args, json_type_object) || json_object_object_length(request) != 2) {
        return reply("COMMAND_NOT_ALLOWED", "文件操作请求非法", "none");
    }
    const char *path = string_field(args, "path");
    bool pair = strcmp(tool, "workspace_copy") == 0 || strcmp(tool, "workspace_move") == 0;
    if (pair) {
        path = string_field(args, "source");
    }
    if (!valid_path(path)) {
        return reply("COMMAND_NOT_ALLOWED", "仅允许工作区内相对路径", "none");
    }
    int fields = json_object_object_length(args);
    if (strcmp(tool, "workspace_list") == 0 &&
        (fields == 1 || (fields == 2 && json_object_object_get(args, "offset") != NULL))) {
        return list_directory(root, path, args);
    }
    char data[FILE_LIMIT + 1];
    size_t length = 0;
    if (strcmp(tool, "workspace_read") == 0 && fields == 1) {
        if (read_text(root, path, data, &length) == 0) {
            return reply(NULL, data, "none");
        }
    } else if (strcmp(tool, "workspace_write") == 0) {
        const char *content = string_field(args, "content");
        json_object *replace = json_object_object_get(args, "overwrite");
        if (content == NULL || strlen(content) > FILE_LIMIT || fields < 2 || fields > 3 ||
            (replace != NULL && !json_object_is_type(replace, json_type_boolean)) ||
            (fields == 3 && replace == NULL)) {
            return reply("COMMAND_NOT_ALLOWED", "写入内容或覆盖参数非法", "none");
        }
        bool overwrite = replace != NULL && json_object_get_boolean(replace);
        if (write_text(root, path, content, overwrite) == 0) {
            return reply(NULL, "文本文件已提交", "committed");
        }
    } else if (pair && fields == 2) {
        const char *destination = string_field(args, "destination");
        if (!valid_path(destination)) {
            return reply("COMMAND_NOT_ALLOWED", "目标路径非法", "none");
        }
        if (strcmp(tool, "workspace_copy") == 0) {
            if (read_text(root, path, data, &length) == 0 &&
                write_text(root, destination, data, false) == 0) {
                return reply(NULL, "文件已复制", "committed");
            }
        } else {
            char source_leaf[129];
            char target_leaf[129];
            int source = parent_fd(root, path, source_leaf);
            int target = parent_fd(root, destination, target_leaf);
            struct stat info;
            int status = -1;
            errno = EPERM;
            if (source >= 0 && target >= 0 &&
                fstatat(source, source_leaf, &info, AT_SYMLINK_NOFOLLOW) == 0 &&
                !S_ISLNK(info.st_mode)) {
                status = (int)syscall(SYS_renameat2, source, source_leaf,
                                      target, target_leaf, RENAME_NOREPLACE);
            }
            if (source >= 0) {
                close(source);
            }
            if (target >= 0) {
                close(target);
            }
            if (status == 0) {
                return reply(NULL, "路径已移动", "committed");
            }
        }
    } else if (fields == 1 &&
               (strcmp(tool, "workspace_mkdir") == 0 || strcmp(tool, "workspace_remove") == 0)) {
        char leaf[129];
        int parent = parent_fd(root, path, leaf);
        if (parent >= 0) {
            int status = -1;
            if (strcmp(tool, "workspace_mkdir") == 0) {
                unsigned count = 0;
                if (count_entries(root, &count) == 0 && count < ENTRY_LIMIT) {
                    status = mkdirat(parent, leaf, 0700);
                } else {
                    errno = EDQUOT;
                }
            } else {
                errno = EPERM;
                struct stat info;
                if (fstatat(parent, leaf, &info, AT_SYMLINK_NOFOLLOW) == 0 &&
                    (S_ISREG(info.st_mode) || S_ISDIR(info.st_mode))) {
                    status = unlinkat(parent, leaf, S_ISDIR(info.st_mode) ? AT_REMOVEDIR : 0);
                }
            }
            int saved = errno;
            close(parent);
            errno = saved;
            if (status == 0) {
                return reply(NULL, "操作已提交", "committed");
            }
        }
    } else {
        return reply("COMMAND_NOT_ALLOWED", "操作或参数不在白名单内", "none");
    }
    const char *code = "COMMAND_FAILED";
    switch (errno) {
    case EFBIG:
    case EDQUOT:
    case ENOSPC:
        code = "COMMAND_OUTPUT_LIMIT";
        break;
    case EPERM:
    case EACCES:
    case ENOENT:
    case EEXIST:
    case ENOTDIR:
    case EISDIR:
    case ENOTEMPTY:
    case ELOOP:
    case EXDEV:
    case EINVAL:
    case ENAMETOOLONG:
        code = "COMMAND_NOT_ALLOWED";
        break;
    default:
        break;
    }
    return reply(code, strerror(errno), "none");
}
