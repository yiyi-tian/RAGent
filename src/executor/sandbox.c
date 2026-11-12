#define _GNU_SOURCE
#include "executor/command.h"
#include "protocol/protocol.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t cancelled = 0;
static char endpoint[512];
static char state_path[4096];
static char lock_path[4096];
static unsigned timeout_ms = 5000;
static int state_lock = -1;
static char scope_label[64];

static void stop_manager(int signal_number)
{
    (void)signal_number;
    cancelled = 1;
}

static json_object *error_result(const char *code, const char *message, const char *effect)
{
    json_object *result = json_object_new_object();
    json_object_object_add(result, "ok", json_object_new_boolean(false));
    json_object_object_add(result, "code", json_object_new_string(code));
    json_object_object_add(result, "output", json_object_new_string(message));
    json_object_object_add(result, "message", json_object_new_string(message));
    json_object_object_add(result, "effect", json_object_new_string(effect));
    json_object_object_add(result, "exit_code", json_object_new_int(-1));
    json_object_object_add(result, "signal", json_object_new_int(0));
    return result;
}

static json_object *ok_result(const char *text)
{
    json_object *result = error_result("", text, "none");
    json_object_object_add(result, "ok", json_object_new_boolean(true));
    json_object_object_add(result, "exit_code", json_object_new_int(0));
    return result;
}

static const char *string_field(json_object *object, const char *name)
{
    json_object *value = NULL;
    if (object == NULL || !json_object_object_get_ex(object, name, &value) ||
        !json_object_is_type(value, json_type_string)) {
        return NULL;
    }
    const char *text = json_object_get_string(value);
    return strlen(text) == (size_t)json_object_get_string_len(value) ? text : NULL;
}

static bool hex_id(const char *value)
{
    if (value == NULL || strlen(value) != 32) {
        return false;
    }
    for (size_t i = 0; i < 32; ++i) {
        if (!isdigit((unsigned char)value[i]) && !(value[i] >= 'a' && value[i] <= 'f')) {
            return false;
        }
    }
    return true;
}

static bool connection_id(const char *value)
{
    if (value == NULL || strlen(value) > 20 || *value == '\0' || *value == '0') {
        return false;
    }
    for (const char *p = value; *p != '\0'; ++p) {
        if (!isdigit((unsigned char)*p)) {
            return false;
        }
    }
    errno = 0;
    strtoull(value, NULL, 10);
    return errno == 0;
}

static int docker(char *const arguments[], const char *input, CommandResult *result,
                   const volatile sig_atomic_t *stop)
{
    char *argv[64] = {"docker", "--host", endpoint};
    size_t count = 3;
    for (size_t i = 0; arguments[i] != NULL; ++i) {
        if (count == 63) {
            return -1;
        }
        argv[count++] = arguments[i];
    }
    argv[count] = NULL;
    return command_capture("/usr/bin/docker", argv, input, input == NULL ? 0 : strlen(input),
                           COMMAND_CAPTURE_LIMIT, timeout_ms, stop, result);
}

static json_object *docker_json(char *const arguments[])
{
    CommandResult result;
    if (docker(arguments, NULL, &result, &cancelled) != 0) {
        return NULL;
    }
    return json_parse_object((const unsigned char *)result.output, result.output_length);
}

static json_object *read_state(void)
{
    int fd = open(state_path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) {
        if (errno != ENOENT) {
            return NULL;
        }
        json_object *state = json_object_new_object();
        json_object_object_add(state, "servers", json_object_new_object());
        json_object_object_add(state, "sessions", json_object_new_object());
        return state;
    }
    unsigned char *buffer = malloc(1048576);
    if (buffer == NULL) {
        close(fd);
        return NULL;
    }
    size_t length = 0;
    while (length < 1048576) {
        ssize_t count = read(fd, buffer + length, 1048576 - length);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            break;
        }
        length += (size_t)count;
    }
    close(fd);
    json_object *state = length < 1048576 ? json_parse_object(buffer, length) : NULL;
    free(buffer);
    if (state == NULL ||
        !json_object_is_type(json_object_object_get(state, "servers"), json_type_object) ||
        !json_object_is_type(json_object_object_get(state, "sessions"), json_type_object)) {
        if (state != NULL) {
            json_object_put(state);
        }
        return NULL;
    }
    return state;
}

static int save_state(json_object *state)
{
    char temporary[4160];
    snprintf(temporary, sizeof(temporary), "%s.%ld", state_path, (long)getpid());
    int fd = open(temporary, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        return -1;
    }
    const char *encoded = json_object_to_json_string_ext(state, JSON_C_TO_STRING_PLAIN);
    size_t length = strlen(encoded);
    size_t sent = 0;
    while (sent < length) {
        ssize_t count = write(fd, encoded + sent, length - sent);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            close(fd);
            unlink(temporary);
            return -1;
        }
        sent += (size_t)count;
    }
    int status = close(fd);
    if (status == 0) {
        status = rename(temporary, state_path);
    }
    if (status != 0) {
        unlink(temporary);
    }
    return status;
}

static bool server_alive(json_object *server)
{
    json_object *pid = json_object_object_get(server, "pid");
    const char *expected = string_field(server, "start");
    const char *boot = string_field(server, "boot");
    if (pid == NULL || !json_object_is_type(pid, json_type_int) ||
        json_object_get_int64(pid) < 1 || expected == NULL || boot == NULL) {
        return false;
    }
    char path[128];
    snprintf(path, sizeof(path), "/proc/%lld/stat", (long long)json_object_get_int64(pid));
    FILE *file = fopen(path, "re");
    char line[4096];
    if (file == NULL) {
        return false;
    }
    char *read = fgets(line, sizeof(line), file);
    fclose(file);
    if (read == NULL) {
        return false;
    }
    char *p = strrchr(line, ')');
    if (p == NULL) {
        return false;
    }
    char *context = NULL;
    char *field = strtok_r(p + 2, " ", &context);
    if (field == NULL || *field == 'Z' || *field == 'X') {
        return false;
    }
    for (int index = 3; field != NULL && index < 22; ++index) {
        field = strtok_r(NULL, " ", &context);
    }
    if (field == NULL || strcmp(field, expected) != 0) {
        return false;
    }
    file = fopen("/proc/sys/kernel/random/boot_id", "re");
    if (file == NULL) {
        return false;
    }
    read = fgets(line, sizeof(line), file);
    fclose(file);
    if (read == NULL) {
        return false;
    }
    line[strcspn(line, "\n")] = '\0';
    return strcmp(line, boot) == 0;
}

static bool active_connection(json_object *server, const char *connection)
{
    if (server == NULL || !server_alive(server)) {
        return false;
    }
    json_object *through = json_object_object_get(server, "through");
    json_object *active = json_object_object_get(server, "active");
    if (!json_object_is_type(through, json_type_string) ||
        !json_object_is_type(active, json_type_array)) {
        return false;
    }
    if (strtoull(connection, NULL, 10) > strtoull(json_object_get_string(through), NULL, 10)) {
        return true;
    }
    size_t count = json_object_array_length(active);
    for (size_t i = 0; i < count; ++i) {
        const char *id = json_object_get_string(json_object_array_get_idx(active, i));
        if (strcmp(id, connection) == 0) {
            return true;
        }
    }
    return false;
}

static bool remove_container(const char *name)
{
    CommandResult result;
    volatile sig_atomic_t cleanup_cancelled = 0;
    char *inspect[] = {
        "container",
        "inspect",
        (char *)name,
        "--format",
        "{{index .Config.Labels \"org.ragent.scope\"}}",
        NULL,
    };
    unsigned saved = timeout_ms;
    timeout_ms = 2000;
    if (docker(inspect, NULL, &result, &cleanup_cancelled) != 0) {
        // inspect失败不一定代表不存在；列表确认名称缺失后才算已清理。
        char filter[128];
        snprintf(filter, sizeof(filter), "name=^/%s$", name);
        char *check[] = {"ps", "--all", "--filter", filter, "--format", "{{.Names}}", NULL};
        bool absent = docker(check, NULL, &result, &cleanup_cancelled) == 0 &&
                      result.output_length == 0;
        timeout_ms = saved;
        return absent;
    }
    result.output[strcspn(result.output, "\r\n")] = '\0';
    if (strcmp(result.output, scope_label + strlen("org.ragent.scope=")) != 0) {
        timeout_ms = saved;
        return false;
    }
    char *args[] = {"rm", "--force", (char *)name, NULL};
    bool removed = docker(args, NULL, &result, &cleanup_cancelled) == 0;
    timeout_ms = saved;
    return removed;
}

static void sweep(json_object *state)
{
    json_object *servers = json_object_object_get(state, "servers");
    json_object *sessions = json_object_object_get(state, "sessions");
    char remove[8][80];
    size_t count = 0;
    json_object_object_foreach(sessions, name, session) {
        const char *owner = string_field(session, "server");
        const char *cid = string_field(session, "connection");
        json_object *server = owner == NULL ? NULL : json_object_object_get(servers, owner);
        json_object *busy = json_object_object_get(session, "busy_pid");
        if (busy != NULL && kill((pid_t)json_object_get_int64(busy), 0) != 0 && errno == ESRCH) {
            json_object_object_add(session, "closed", json_object_new_boolean(true));
        }
        if (cid == NULL || !active_connection(server, cid) ||
            json_object_get_boolean(json_object_object_get(session, "closed"))) {
            bool active = cid != NULL && active_connection(server, cid);
            if (count < 8 && remove_container(name) && !active) {
                snprintf(remove[count++], sizeof(remove[0]), "%s", name);
            }
        }
    }
    for (size_t i = 0; i < count; ++i) {
        json_object_object_del(sessions, remove[i]);
    }
    char dead[32][33];
    count = 0;
    json_object_object_foreach(servers, id, server) {
        if (!server_alive(server) && count < 32) {
            snprintf(dead[count++], sizeof(dead[0]), "%s", id);
        }
    }
    for (size_t i = 0; i < count; ++i) {
        json_object_object_del(servers, dead[i]);
    }
}

static bool sweep_orphans(json_object *state)
{
    CommandResult result;
    char filter[80];
    snprintf(filter, sizeof(filter), "label=%s", scope_label);
    char *args[] = {"ps", "--all", "--filter", filter, "--format", "{{.Names}}", NULL};
    if (docker(args, NULL, &result, &cancelled) != 0) {
        return false;
    }
    json_object *sessions = json_object_object_get(state, "sessions");
    char *context = NULL;
    for (char *name = strtok_r(result.output, "\n", &context); name != NULL;
         name = strtok_r(NULL, "\n", &context)) {
        if (json_object_object_get(sessions, name) == NULL && !remove_container(name)) {
            return false;
        }
    }
    return true;
}

static bool rootless_available(void)
{
    json_object *info = docker_json((char *[]) {"info", "--format", "{{json .}}", NULL});
    if (info == NULL) {
        return false;
    }
    bool rootless = false;
    bool seccomp = false;
    json_object *options = json_object_object_get(info, "SecurityOptions");
    if (json_object_is_type(options, json_type_array)) {
        for (size_t i = 0; i < json_object_array_length(options); ++i) {
            const char *value = json_object_get_string(json_object_array_get_idx(options, i));
            rootless = rootless || strncmp(value, "name=rootless", 13) == 0;
            seccomp = seccomp || strncmp(value, "name=seccomp", 12) == 0;
        }
    }
    const char *version = string_field(info, "CgroupVersion");
    const char *driver = string_field(info, "CgroupDriver");
    bool ok = rootless && seccomp && version != NULL && strcmp(version, "2") == 0 &&
              driver != NULL && strcmp(driver, "systemd") == 0;
    json_object_put(info);
    return ok;
}

static json_object *create_session(json_object **state_pointer, const char *name,
                                   const char *server_id, const char *cid, bool parallel)
{
    json_object *state = *state_pointer;
    json_object *sessions = json_object_object_get(state, "sessions");
    json_object *session = json_object_object_get(sessions, name);
    if (session != NULL) {
        return session;
    }
    if (json_object_object_length(sessions) >= 8) {
        return NULL;
    }
    const char *image = string_field(state, "image");
    if (image == NULL) {
        return NULL;
    }
    char label[96];
    snprintf(label, sizeof(label), "org.ragent.server=%s", server_id);
    char *args[] = {
        "create",
        "--name",
        (char *)name,
        "--label",
        "org.ragent.sandbox=true",
        "--label",
        label,
        "--label",
        scope_label,
        "--network",
        "none",
        "--read-only",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--memory",
        "128m",
        "--memory-swap",
        "128m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "32",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000,mode=0700",
        (char *)image,
        NULL,
    };
    session = json_object_new_object();
    json_object_object_add(session, "server", json_object_new_string(server_id));
    json_object_object_add(session, "connection", json_object_new_string(cid));
    json_object_object_add(sessions, name, session);
    // 先记录确定的容器名，使创建中断后仍可对账回收。
    if (save_state(state) != 0) {
        json_object_object_del(sessions, name);
        return NULL;
    }
    if (parallel && flock(state_lock, LOCK_UN) != 0) {
        return NULL;
    }
    CommandResult result;
    bool ok = true;
    if (docker(args, NULL, &result, &cancelled) != 0 ||
        docker((char *[]) {"start", (char *)name, NULL}, NULL, &result, &cancelled) != 0) {
        ok = false;
    }
    char *probe_args[] = {
        "exec",
        (char *)name,
        "/usr/local/bin/ragent-file-worker",
        "--probe",
        NULL,
    };
    if (ok && docker(probe_args, NULL, &result, &cancelled) != 0) {
        ok = false;
    }
    if (parallel) {
        if (flock(state_lock, LOCK_EX) != 0) {
            _exit(1);
        }
        json_object_put(state);
        state = read_state();
        *state_pointer = state;
        if (state == NULL) {
            remove_container(name);
            return NULL;
        }
        sessions = json_object_object_get(state, "sessions");
        session = json_object_object_get(sessions, name);
    }
    if (!ok || session == NULL) {
        if (session != NULL) {
            json_object_object_add(session, "closed", json_object_new_boolean(true));
        }
        remove_container(name);
        return NULL;
    }
    return session;
}

static json_object *invoke(json_object **state_pointer, json_object *request)
{
    json_object *state = *state_pointer;
    json_object *context = json_object_object_get(request, "session");
    const char *server_id = string_field(context, "server_id");
    const char *cid = string_field(context, "connection_id");
    const char *tool = string_field(request, "tool");
    json_object *args = json_object_object_get(request, "args");
    json_object *server = hex_id(server_id) ?
        json_object_object_get(json_object_object_get(state, "servers"), server_id) : NULL;
    if (!json_object_is_type(context, json_type_object) ||
        json_object_object_length(context) != 2 || !connection_id(cid) || tool == NULL ||
        strncmp(tool, "workspace_", 10) != 0 ||
        !json_object_is_type(args, json_type_object) || !active_connection(server, cid)) {
        return error_result("COMMAND_NOT_ALLOWED", "连接工作区不可用，请重新连接", "none");
    }
    time_t seen = json_object_get_int64(json_object_object_get(server, "seen"));
    if (time(NULL) - seen > 10) {
        return error_result("UPSTREAM_UNAVAILABLE", "连接维护已失联，文件操作暂停", "none");
    }
    char name[80];
    snprintf(name, sizeof(name), "ragent-ws-%s-%s", server_id, cid);
    json_object *sessions = json_object_object_get(state, "sessions");
    if (json_object_object_get(sessions, name) == NULL &&
        json_object_object_length(sessions) >= 8) {
        return error_result("SERVER_BUSY", "临时工作区已满，请稍后使用", "none");
    }
    json_object *session = create_session(state_pointer, name, server_id, cid, true);
    state = *state_pointer;
    if (session == NULL) {
        return error_result("UPSTREAM_UNAVAILABLE", "沙箱创建失败，未执行文件操作", "none");
    }
    if (json_object_object_get(session, "busy_pid") != NULL) {
        // 已获得连接锁但仍有执行记录，说明前一管理进程异常退出；不再复用该工作区。
        json_object_object_add(session, "closed", json_object_new_boolean(true));
    }
    if (json_object_get_boolean(json_object_object_get(session, "closed"))) {
        return error_result("COMMAND_NOT_ALLOWED", "工作区已关闭，请重新连接", "none");
    }
    json_object *payload = json_object_new_object();
    json_object_object_add(payload, "tool", json_object_new_string(tool));
    json_object_object_add(payload, "args", json_object_get(args));
    const char *input = json_object_to_json_string_ext(payload, JSON_C_TO_STRING_PLAIN);
    CommandResult captured;
    char *arguments[] = {"exec", "-i", name, "/usr/local/bin/ragent-file-worker", NULL};
    json_object_object_add(session, "busy_pid", json_object_new_int64(getpid()));
    // 每个连接已持有独立锁；执行期间释放全局状态锁，让其他连接和维护继续推进。
    if (save_state(state) != 0 || flock(state_lock, LOCK_UN) != 0) {
        json_object_put(payload);
        return error_result("COMMAND_FAILED", "无法保存执行前状态", "none");
    }
    int status = docker(arguments, input, &captured, &cancelled);
    json_object_put(payload);
    // 同步可能在执行期间关闭连接，必须重新读取，不能覆盖更新后的快照。
    if (flock(state_lock, LOCK_EX) != 0) {
        _exit(1);
    }
    json_object_put(state);
    state = read_state();
    *state_pointer = state;
    if (state == NULL) {
        return error_result("COMMAND_FAILED", "执行后状态不可读，实际状态未知", "unknown");
    }
    sessions = json_object_object_get(state, "sessions");
    session = json_object_object_get(sessions, name);
    if (session == NULL) {
        return error_result("COMMAND_FAILED", "连接已关闭，工作区已回收", "unknown");
    }
    if (status != 0) {
        json_object_object_add(session, "closed", json_object_new_boolean(true));
        save_state(state);
        remove_container(name);
        const char *code = captured.code == NULL ? "COMMAND_FAILED" : captured.code;
        return error_result(code, "文件执行中断，状态未知；工作区已关闭，请重新连接", "unknown");
    }
    json_object *result = json_parse_object((const unsigned char *)captured.output,
                                           captured.output_length);
    if (result == NULL) {
        json_object_object_add(session, "closed", json_object_new_boolean(true));
        return error_result("COMMAND_FAILED", "沙箱返回非法响应，实际状态未知", "unknown");
    }
    json_object_object_del(session, "busy_pid");
    return result;
}

static json_object *sync_sessions(json_object *state, json_object *request)
{
    json_object *snapshot = json_object_object_get(request, "snapshot");
    const char *id = string_field(snapshot, "server_id");
    const char *through = string_field(snapshot, "through");
    json_object *sequence = json_object_object_get(snapshot, "sequence");
    json_object *active = json_object_object_get(snapshot, "active");
    bool valid_through = through != NULL &&
        (strcmp(through, "0") == 0 || connection_id(through));
    if (!hex_id(id) || !valid_through ||
        !json_object_is_type(sequence, json_type_int) || json_object_get_int64(sequence) < 1 ||
        !json_object_is_type(active, json_type_array) || json_object_array_length(active) > 4096 ||
        !server_alive(snapshot)) {
        return error_result("COMMAND_NOT_ALLOWED", "连接快照非法", "none");
    }
    for (size_t i = 0; i < json_object_array_length(active); ++i) {
        json_object *item = json_object_array_get_idx(active, i);
        if (!json_object_is_type(item, json_type_string) ||
            !connection_id(json_object_get_string(item))) {
            return error_result("COMMAND_NOT_ALLOWED", "连接标识非法", "none");
        }
    }
    json_object *servers = json_object_object_get(state, "servers");
    json_object *previous = json_object_object_get(servers, id);
    if (previous == NULL && json_object_object_length(servers) >= 32) {
        return error_result("SERVER_BUSY", "连接服务实例已满", "none");
    }
    if (previous == NULL || json_object_get_int64(sequence) >
        json_object_get_int64(json_object_object_get(previous, "sequence"))) {
        json_object_object_add(snapshot, "seen", json_object_new_int64(time(NULL)));
        json_object_object_add(servers, id, json_object_get(snapshot));
    }
    sweep(state);
    return ok_result("连接快照已同步");
}

static int audit(json_object *request, json_object *result)
{
    char path[4160];
    snprintf(path, sizeof(path), "%s.audit", state_path);
    struct stat info;
    if (stat(path, &info) == 0 && info.st_size >= 8 * 1024 * 1024) {
        char first[4200];
        char second[4200];
        snprintf(first, sizeof(first), "%s.1", path);
        snprintf(second, sizeof(second), "%s.2", path);
        if (unlink(second) != 0 && errno != ENOENT) {
            return -1;
        }
        if (rename(first, second) != 0 && errno != ENOENT) {
            return -1;
        }
        if (rename(path, first) != 0) {
            return -1;
        }
    }
    int fd = open(path, O_CREAT | O_APPEND | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        return -1;
    }
    json_object *record = json_object_new_object();
    const char *keys[] = {"tool", "operation_id", "request_id", NULL};
    for (size_t i = 0; keys[i] != NULL; ++i) {
        const char *value = string_field(request, keys[i]);
        if (value != NULL && strlen(value) <= 128) {
            json_object_object_add(record, keys[i], json_object_new_string(value));
        }
    }
    json_object *context = json_object_object_get(request, "session");
    const char *server = string_field(context, "server_id");
    const char *connection = string_field(context, "connection_id");
    if (hex_id(server) && connection_id(connection)) {
        json_object_object_add(record, "server_id", json_object_new_string(server));
        json_object_object_add(record, "connection_id", json_object_new_string(connection));
    }
    json_object *args = json_object_object_get(request, "args");
    const char *paths[] = {"path", "source", "destination", NULL};
    for (size_t i = 0; paths[i] != NULL; ++i) {
        const char *value = string_field(args, paths[i]);
        if (value != NULL && strlen(value) <= 512) {
            json_object_object_add(record, paths[i], json_object_new_string(value));
        }
    }
    const char *result_keys[] = {
        "ok",
        "code",
        "effect",
        "exit_code",
        "signal",
        "duration_ms",
        NULL,
    };
    for (size_t i = 0; result_keys[i] != NULL; ++i) {
        json_object *value = json_object_object_get(result, result_keys[i]);
        if (value != NULL) {
            json_object_object_add(record, result_keys[i], json_object_get(value));
        }
    }
    json_object_object_add(record, "time", json_object_new_int64(time(NULL)));
    const char *text = json_object_to_json_string_ext(record, JSON_C_TO_STRING_PLAIN);
    int status = dprintf(fd, "%s\n", text) < 0 ? -1 : 0;
    if (close(fd) != 0) {
        status = -1;
    }
    json_object_put(record);
    return status;
}

int main(int argc, char **argv)
{
    umask(0077);
    const char *socket_path = NULL;
    const char *state_file = NULL;
    for (int i = 1; i + 1 < argc; i += 2) {
        if (strcmp(argv[i], "--socket") == 0) {
            socket_path = argv[i + 1];
        } else if (strcmp(argv[i], "--state") == 0) {
            state_file = argv[i + 1];
        } else {
            return 1;
        }
    }
    struct stat socket_info;
    if (socket_path == NULL || state_file == NULL || strlen(socket_path) > 480 ||
        strlen(state_file) > 4000 || stat(socket_path, &socket_info) != 0 ||
        !S_ISSOCK(socket_info.st_mode) || socket_info.st_uid != getuid()) {
        return 1;
    }
    snprintf(endpoint, sizeof(endpoint), "unix://%s", socket_path);
    snprintf(state_path, sizeof(state_path), "%s", state_file);
    snprintf(lock_path, sizeof(lock_path), "%s.lock", state_file);
    unsigned long long scope = 14695981039346656037ULL;
    for (const unsigned char *p = (const unsigned char *)state_file; *p != '\0'; ++p) {
        scope = (scope ^ *p) * 1099511628211ULL;
    }
    snprintf(scope_label, sizeof(scope_label), "org.ragent.scope=%016llx", scope);
    struct sigaction action = {0};
    action.sa_handler = stop_manager;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0 ||
        sigaction(SIGINT, &action, NULL) != 0 || signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        return 1;
    }
    unsigned char *input = malloc(131072);
    if (input == NULL) {
        return 1;
    }
    size_t length = fread(input, 1, 131072, stdin);
    json_object *request = length < 131072 ? json_parse_object(input, length) : NULL;
    free(input);
    const char *operation = string_field(request, "operation");
    if (operation == NULL) {
        if (request != NULL) {
            json_object_put(request);
        }
        return 1;
    }
    int session_lock = -1;
    if (strcmp(operation, "invoke") == 0) {
        json_object *context = json_object_object_get(request, "session");
        const char *server = string_field(context, "server_id");
        const char *cid = string_field(context, "connection_id");
        if (!hex_id(server) || !connection_id(cid)) {
            json_object_put(request);
            return 1;
        }
        // 固定64个锁槽，避免随连接积累文件；相同连接始终落在同一槽。
        char path[4160];
        unsigned bucket = (unsigned)(strtoull(cid, NULL, 10) % 64);
        snprintf(path, sizeof(path), "%s.slot-%u.lock", state_path, bucket);
        session_lock = open(path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (session_lock < 0 || flock(session_lock, LOCK_EX) != 0) {
            if (session_lock >= 0) {
                close(session_lock);
            }
            json_object_put(request);
            return 1;
        }
    }
    int lock = open(lock_path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (lock < 0 || flock(lock, LOCK_EX) != 0) {
        if (lock >= 0) {
            close(lock);
        }
        if (session_lock >= 0) {
            close(session_lock);
        }
        json_object_put(request);
        return 1;
    }
    state_lock = lock;
    json_object *state = read_state();
    json_object *result = NULL;
    if (state == NULL) {
        result = error_result("COMMAND_FAILED", "沙箱状态文件无效", "none");
    } else if (!rootless_available()) {
        result = error_result("UPSTREAM_UNAVAILABLE", "rootless隔离或资源控制不可用", "none");
    } else if (strcmp(operation, "probe") == 0) {
        sweep(state);
        if (!sweep_orphans(state)) {
            json_object_put(state);
            close(lock);
            json_object_put(request);
            return 1;
        }
        json_object *image = docker_json((char *[]) {
            "image",
            "inspect",
            "ragent-file-sandbox:v1",
            "--format",
            "{{json .}}",
            NULL,
        });
        const char *id = string_field(image, "Id");
        if (id == NULL || strncmp(id, "sha256:", 7) != 0) {
            result = error_result("UPSTREAM_UNAVAILABLE", "沙箱镜像不可用，请先部署", "none");
        } else {
            if (json_object_object_length(json_object_object_get(state, "sessions")) == 0) {
                json_object_object_add(state, "image", json_object_new_string(id));
            }
            const char *validated = string_field(state, "validated_image");
            if (validated != NULL && strcmp(validated, id) == 0) {
                result = ok_result("rootless沙箱管理组件就绪");
            } else {
                char name[80];
                snprintf(name, sizeof(name), "ragent-probe-%ld", (long)getpid());
                json_object *probe = create_session(&state, name,
                    "00000000000000000000000000000000", "1", false);
                bool ok = probe != NULL && remove_container(name);
                json_object_object_del(json_object_object_get(state, "sessions"), name);
                if (ok) {
                    json_object_object_add(state, "validated_image", json_object_new_string(id));
                    result = ok_result("rootless沙箱及隔离探针就绪");
                } else {
                    remove_container(name);
                    result = error_result("UPSTREAM_UNAVAILABLE", "沙箱隔离探针失败", "none");
                }
            }
        }
        if (image != NULL) {
            json_object_put(image);
        }
    } else if (strcmp(operation, "sync") == 0) {
        result = sync_sessions(state, request);
    } else if (strcmp(operation, "invoke") == 0) {
        struct timespec started;
        bool timed = clock_gettime(CLOCK_MONOTONIC, &started) == 0;
        sweep(state);
        result = invoke(&state, request);
        struct timespec ended;
        if (clock_gettime(CLOCK_MONOTONIC, &ended) == 0 && timed) {
            int64_t duration = (ended.tv_sec - started.tv_sec) * 1000 +
                               (ended.tv_nsec - started.tv_nsec) / 1000000;
            json_object_object_add(result, "duration_ms", json_object_new_int64(duration));
        }
        if (audit(request, result) != 0) {
            json_object *effect = json_object_object_get(result, "effect");
            json_object *failed = error_result("COMMAND_FAILED", "操作审计写入失败，请核查实际状态",
                                              effect == NULL ? "unknown"
                                                             : json_object_get_string(effect));
            json_object_put(result);
            result = failed;
        }
    } else {
        result = error_result("COMMAND_NOT_ALLOWED", "未知沙箱管理操作", "none");
    }
    if (state != NULL) {
        if (save_state(state) != 0) {
            json_object_put(result);
            result = error_result("COMMAND_FAILED", "沙箱状态持久化失败，实际状态未知", "unknown");
        }
        json_object_put(state);
    }
    close(lock);
    if (session_lock >= 0) {
        close(session_lock);
    }
    json_object_put(request);
    if (result == NULL) {
        return 1;
    }
    const char *encoded = json_object_to_json_string_ext(result, JSON_C_TO_STRING_PLAIN);
    int status = puts(encoded) < 0 || fflush(stdout) != 0 ? 1 : 0;
    json_object_put(result);
    return status;
}
