#define _GNU_SOURCE

#include "net/socket.h"
#include "protocol/protocol.h"
#include "executor/local.h"
#include "executor/policy.h"
#include "client/terminal.h"
#include <fcntl.h>

#include <errno.h>
#include <math.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

typedef enum { CLIENT_MODE_AGENT, CLIENT_MODE_CHAT, CLIENT_MODE_RAG, CLIENT_MODE_CMD } ClientMode;

typedef struct {
    char host[INET_ADDRSTRLEN];
    unsigned port;
    unsigned timeout_ms;
    ClientMode mode;
    const char *query;
    bool once;
    bool local_policy;
} Options;

typedef struct {
    int socket_fd;
    unsigned timeout_ms;
    uint64_t next_request_id;
    FrameDecoder decoder;
    bool local_policy;
    bool cleanup_failed;
    bool approval_required;
    char consumed[4][33];
    unsigned consumed_count;
    int workspace;
    char workspace_name[4096];
    char observer[4096];
    char sandbox_socket[512];
} Client;

static volatile sig_atomic_t interrupted = 0;

static void interrupt_handler(int signal_number)
{
    (void)signal_number;
    interrupted = 1;
}

static const char *mode_name(ClientMode mode)
{
    switch (mode) {
    case CLIENT_MODE_AGENT:
        return "agent";
    case CLIENT_MODE_CHAT:
        return "chat";
    case CLIENT_MODE_RAG:
        return "rag";
    case CLIENT_MODE_CMD:
        return "cmd";
    }
    return "agent";
}

static MessageType mode_message_type(ClientMode mode)
{
    switch (mode) {
    case CLIENT_MODE_AGENT:
        return MESSAGE_AGENT_REQUEST;
    case CLIENT_MODE_CHAT:
        return MESSAGE_CHAT_REQUEST;
    case CLIENT_MODE_RAG:
        return MESSAGE_RAG_REQUEST;
    case CLIENT_MODE_CMD:
        return MESSAGE_CMD_REQUEST;
    }
    return MESSAGE_AGENT_REQUEST;
}

static int parse_mode(const char *value, ClientMode *mode)
{
    if (strcmp(value, "agent") == 0) {
        *mode = CLIENT_MODE_AGENT;
    } else if (strcmp(value, "chat") == 0) {
        *mode = CLIENT_MODE_CHAT;
    } else if (strcmp(value, "rag") == 0) {
        *mode = CLIENT_MODE_RAG;
    } else if (strcmp(value, "cmd") == 0) {
        *mode = CLIENT_MODE_CMD;
    } else {
        return -1;
    }
    return 0;
}

static int parse_options(int argc, char **argv, Options *options)
{
    memset(options, 0, sizeof(*options));
    strcpy(options->host, "127.0.0.1");
    options->port = 9000;
    options->timeout_ms = 130000;
    options->mode = CLIENT_MODE_AGENT;
    options->local_policy = true;
    bool has_mode = false;
    bool has_query = false;
    for (int index = 1; index < argc; ++index) {
        const char *argument = argv[index];
        if (strcmp(argument, "--remote-only") == 0) {
            options->local_policy = false;
            continue;
        }
        if (index + 1 >= argc) {
            fprintf(stderr, "参数缺少值: %s\n", argument);
            return -1;
        }
        const char *value = argv[++index];
        unsigned parsed = 0;
        if (strcmp(argument, "--host") == 0) {
            struct sockaddr_in address;
            if (strlen(value) >= sizeof(options->host) ||
                set_ipv4_address(value, 0, &address) != 0) {
                fprintf(stderr, "host必须是IPv4地址\n");
                return -1;
            }
            strcpy(options->host, value);
        } else if (strcmp(argument, "--port") == 0) {
            if (parse_unsigned(value, 1, 65535, &parsed) != 0) {
                goto invalid_number;
            }
            options->port = parsed;
        } else if (strcmp(argument, "--timeout-ms") == 0) {
            if (parse_unsigned(value, 1, 3600000, &parsed) != 0) {
                goto invalid_number;
            }
            options->timeout_ms = parsed;
        } else if (strcmp(argument, "--mode") == 0) {
            if (parse_mode(value, &options->mode) != 0) {
                fprintf(stderr, "mode必须为agent、chat、rag或cmd\n");
                return -1;
            }
            has_mode = true;
        } else if (strcmp(argument, "--query") == 0) {
            options->query = value;
            has_query = true;
        } else {
            fprintf(stderr, "未知参数: %s\n", argument);
            return -1;
        }
        continue;
    invalid_number:
        fprintf(stderr, "数值参数格式错误或超出范围: %s\n", argument);
        return -1;
    }
    if (has_mode != has_query) {
        fprintf(stderr, "--mode与--query必须同时提供\n");
        return -1;
    }
    options->once = has_query;
    if (options->once && text_is_blank(options->query, strlen(options->query))) {
        fprintf(stderr, "query不能为空白\n");
        return -1;
    }
    return 0;
}

static int wait_socket(int fd, short events, int64_t deadline_ms, int64_t request_started_ms)
{
    bool shown = false;
    unsigned step = 0;
    int result = -1;
    while (!interrupted) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
            break;
        }
        const int64_t current_ms = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
        const int64_t remaining = deadline_ms - current_ms;
        if (remaining <= 0) {
            errno = ETIMEDOUT;
            break;
        }
        int poll_ms = remaining > INT_MAX ? INT_MAX : (int)remaining;
        if (request_started_ms >= 0 && poll_ms > 200) {
            poll_ms = 200;
        }
        struct pollfd descriptor = {
            .fd = fd,
            .events = events,
            .revents = 0,
        };
        const int count = poll(&descriptor, 1, poll_ms);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (count == 0) {
            /* 就绪事件直接返回，避免半包连续到达时反复刷新终端。 */
            if (request_started_ms >= 0) {
                if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
                    break;
                }
                const int64_t elapsed_ms = (int64_t)now.tv_sec * 1000 +
                                           now.tv_nsec / 1000000 - request_started_ms;
                if (elapsed_ms >= 500) {
                    const char symbols[] = "|/-\\";
                    fprintf(stderr,
                            "\r\033[2K%c 等待服务响应 %.1f 秒（Ctrl+C 退出等待）",
                            symbols[step++ % 4],
                            elapsed_ms / 1000.0);
                    fflush(stderr);
                    shown = true;
                }
            }
            continue;
        }
        if ((descriptor.revents & (events | POLLERR | POLLHUP)) != 0) {
            result = 0;
            break;
        }
        if ((descriptor.revents & POLLNVAL) != 0) {
            errno = EBADF;
            break;
        }
    }
    int saved_errno = interrupted ? EINTR : errno;
    if (shown) {
        fputs("\r\033[2K", stderr);
        fflush(stderr);
    }
    if (result != 0 && saved_errno == ETIMEDOUT) {
        fprintf(stderr, "网络等待超时，连接关闭；请求执行结果未知，不自动重试\n");
    }
    if (interrupted && request_started_ms >= 0) {
        fprintf(stderr, "已停止等待；服务端任务可能仍在执行，不自动重试。\n");
    }
    errno = saved_errno;
    return result;
}

static int64_t deadline_after(unsigned timeout_ms)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 + timeout_ms;
}

static int client_init(Client *client, const Options *options)
{
    memset(client, 0, sizeof(*client));
    client->socket_fd = -1;
    client->workspace = -1;
    client->local_policy = options->local_policy;
    if (client->local_policy) {
        client->workspace = open(".", O_PATH | O_DIRECTORY | O_CLOEXEC);
        ssize_t length = readlink("/proc/self/exe", client->observer, sizeof(client->observer) - 1);
        if (client->workspace < 0 || length < 0 ||
            (size_t)length >= sizeof(client->observer) - 1 ||
            getcwd(client->workspace_name, sizeof(client->workspace_name)) == NULL) {
            return -1;
        }
        if (policy_protected_path(client->workspace_name)) {
            fputs("不能将凭证或执行器配置目录作为命令工作区。\n", stderr);
            errno = EPERM;
            return -1;
        }
        const char *forbidden[] = {"/", "/home", "/tmp", "/var", "/proc", "/sys", "/dev",
            "/run", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", NULL};
        for (size_t index = 0; forbidden[index] != NULL; ++index) {
            size_t prefix = strlen(forbidden[index]);
            bool subtree = index >= 4;
            if (strcmp(client->workspace_name, forbidden[index]) == 0 ||
                (subtree && strncmp(client->workspace_name, forbidden[index], prefix) == 0 &&
                 client->workspace_name[prefix] == '/')) {
                fputs("不能将系统控制目录作为命令工作区，请进入项目目录后启动。\n", stderr);
                errno = EPERM;
                return -1;
            }
        }
        client->observer[length] = '\0';
        char *slash = strrchr(client->observer, '/');
        if (slash == NULL || (size_t)(slash - client->observer) + 16 >= sizeof(client->observer)) {
            return -1;
        }
        strcpy(slash + 1, "ragent-observe");
        snprintf(client->sandbox_socket, sizeof(client->sandbox_socket),
                 "/run/user/%lu/ragent-sandbox-docker.sock", (unsigned long)getuid());
    }
    client->socket_fd = make_ipv4_socket();
    client->timeout_ms = options->timeout_ms;
    client->next_request_id = 1;
    frame_decoder_init(&client->decoder);
    if (client->socket_fd < 0) {
        return -1;
    }
    struct sockaddr_in address;
    if (set_ipv4_address(options->host, options->port, &address) != 0) {
        goto failure;
    }
    const int result = connect(client->socket_fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) {
        goto failure;
    }
    if (result < 0) {
        if (wait_socket(client->socket_fd, POLLOUT, deadline_after(client->timeout_ms), -1) != 0) {
            goto failure;
        }
        int socket_error = 0;
        socklen_t error_size = sizeof(socket_error);
        if (getsockopt(client->socket_fd, SOL_SOCKET, SO_ERROR, &socket_error, &error_size) != 0) {
            goto failure;
        }
        if (socket_error != 0) {
            errno = socket_error;
            goto failure;
        }
    }
    return 0;

failure:
    {
        const int saved_errno = errno;
        close(client->socket_fd);
        client->socket_fd = -1;
        frame_decoder_destroy(&client->decoder);
        errno = saved_errno;
        return -1;
    }
}

static void client_destroy(Client *client)
{
    if (client->workspace >= 0) {
        close(client->workspace);
        client->workspace = -1;
    }
    if (client->socket_fd >= 0) {
        close(client->socket_fd);
    }
    frame_decoder_destroy(&client->decoder);
    client->socket_fd = -1;
}

static int display_response(const Frame *frame, MessageType request_type)
{
    json_object *body = json_parse_object(frame->body, frame->body_length);
    if (body == NULL) {
        fprintf(stderr, "服务端返回非法JSON响应\n");
        return -1;
    }
    if (frame->type == MESSAGE_ERROR) {
        json_object *code = NULL;
        json_object *message = NULL;
        if (!json_object_object_get_ex(body, "code", &code) ||
            !json_object_is_type(code, json_type_string) ||
            !json_object_object_get_ex(body, "message", &message) ||
            !json_object_is_type(message, json_type_string)) {
            json_object_put(body);
            fprintf(stderr, "非法错误响应\n");
            return -1;
        }
        terminal_write(stderr, (const unsigned char *)json_object_get_string(code),
                       (size_t)json_object_get_string_len(code));
        fputs(": ", stderr);
        terminal_write(stderr, (const unsigned char *)json_object_get_string(message),
                       (size_t)json_object_get_string_len(message));
        fputc('\n', stderr);
        json_object_put(body);
        return 1;
    }
    if (request_type == MESSAGE_PING && frame->type == MESSAGE_PONG) {
        puts("Agent 网络连接正常（未检查依赖服务）。");
        json_object_put(body);
        return 0;
    }
    if (request_type == MESSAGE_PING || frame->type != MESSAGE_RESPONSE) {
        json_object_put(body);
        fprintf(stderr, "非法响应类型\n");
        return -1;
    }
    json_object *text = NULL;
    json_object *sources = NULL;
    if (!json_object_object_get_ex(body, "text", &text) ||
        !json_object_is_type(text, json_type_string) ||
        !json_object_object_get_ex(body, "sources", &sources) ||
        !json_object_is_type(sources, json_type_array)) {
        json_object_put(body);
        fprintf(stderr, "非法成功响应\n");
        return -1;
    }
    for (size_t index = 0; index < json_object_array_length(sources); ++index) {
        json_object *source = json_object_array_get_idx(sources, index);
        json_object *name = NULL;
        json_object *chunk_id = NULL;
        json_object *excerpt = NULL;
        json_object *score = NULL;
        if (!json_object_is_type(source, json_type_object) ||
            !json_object_object_get_ex(source, "source", &name) ||
            !json_object_is_type(name, json_type_string) ||
            !json_object_object_get_ex(source, "chunk_id", &chunk_id) ||
            !json_object_is_type(chunk_id, json_type_string) ||
            !json_object_object_get_ex(source, "text", &excerpt) ||
            !json_object_is_type(excerpt, json_type_string) ||
            !json_object_object_get_ex(source, "score", &score) ||
            !(json_object_is_type(score, json_type_double) ||
              json_object_is_type(score, json_type_int)) ||
            !isfinite(json_object_get_double(score))) {
            json_object_put(body);
            fprintf(stderr, "非法检索来源\n");
            return -1;
        }
    }
    terminal_write(stdout, (const unsigned char *)json_object_get_string(text),
                   (size_t)json_object_get_string_len(text));
    putchar('\n');
    for (size_t index = 0; index < json_object_array_length(sources); ++index) {
        json_object *source = json_object_array_get_idx(sources, index);
        json_object *name = json_object_object_get(source, "source");
        json_object *chunk = json_object_object_get(source, "chunk_id");
        fputs("来源：", stdout);
        terminal_write(stdout, (const unsigned char *)json_object_get_string(name),
                       (size_t)json_object_get_string_len(name));
        fputs(" [", stdout);
        terminal_write(stdout, (const unsigned char *)json_object_get_string(chunk),
                       (size_t)json_object_get_string_len(chunk));
        fputs("]\n", stdout);
    }
    json_object_put(body);
    return 0;
}

static int send_json(Client *client, MessageType type, uint64_t id, json_object *body)
{
    const char *text = json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN);
    Frame frame = {
        .type = type,
        .request_id = id,
        .body = (unsigned char *)text,
        .body_length = strlen(text),
    };
    unsigned char *encoded = NULL;
    size_t length;
    if (frame_encode(&frame, &encoded, &length) != 0) {
        return -1;
    }
    size_t offset = 0;
    int64_t deadline = deadline_after(client->timeout_ms);
    while (offset < length) {
        if (wait_socket(client->socket_fd, POLLOUT, deadline, -1) != 0) {
            free(encoded);
            return -1;
        }
        ssize_t count = send(client->socket_fd, encoded + offset, length - offset, MSG_NOSIGNAL);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) {
            continue;
        }
        if (count <= 0) {
            free(encoded);
            return -1;
        }
        offset += (size_t)count;
    }
    free(encoded);
    return 0;
}

static int receive_frame(Client *client, Frame *frame)
{
    int64_t deadline = deadline_after(client->timeout_ms);
    while (!interrupted) {
        const char *error = NULL;
        int decoded = frame_decoder_next(&client->decoder, frame, &error);
        if (decoded > 0) {
            return 0;
        }
        if (decoded < 0 || wait_socket(client->socket_fd, POLLIN, deadline, -1) != 0) {
            return -1;
        }
        unsigned char bytes[16384];
        ssize_t count = recv(client->socket_fd, bytes, sizeof(bytes), 0);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) {
            continue;
        }
        if (count <= 0 || frame_decoder_append(&client->decoder, bytes, (size_t)count) != 0) {
            return -1;
        }
    }
    return -1;
}

typedef struct {
    Client *client;
    uint64_t id;
    const char *token;
    bool live;
} LocalTask;

static bool task_alive(void *context)
{
    LocalTask *task = context;
    json_object *body = json_object_new_object();
    json_object_object_add(body, "continuation", json_object_new_string(task->token));
    int sent = send_json(task->client, MESSAGE_TASK_HEARTBEAT, task->id, body);
    json_object_put(body);
    Frame response = {0};
    if (sent != 0 || receive_frame(task->client, &response) != 0) {
        task->live = false;
        return false;
    }
    bool valid = response.request_id == task->id && response.type == MESSAGE_TASK_STATUS;
    json_object *value = valid ? json_parse_object(response.body, response.body_length) : NULL;
    valid = value != NULL && frame_decoder_empty(&task->client->decoder) &&
        json_object_get_boolean(json_object_object_get(value, "active"));
    if (value != NULL) {
        json_object_put(value);
    }
    if (!valid && response.type == MESSAGE_ERROR) {
        display_response(&response, MESSAGE_CMD_REQUEST);
    }
    frame_destroy(&response);
    task->live = valid;
    return valid;
}

static bool task_connected(void *context)
{
    LocalTask *task = context;
    if (!task->live || !frame_decoder_empty(&task->client->decoder)) {
        return false;
    }
    unsigned char byte;
    ssize_t count = recv(task->client->socket_fd, &byte, 1, MSG_PEEK | MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) {
        return true;
    }
    task->live = false;
    return false;
}

static int execute_proposals(Client *client, const Frame *response)
{
    json_object *body = json_parse_object(response->body, response->body_length);
    json_object *commands = body == NULL ? NULL : json_object_object_get(body, "commands");
    json_object *token = body == NULL ? NULL : json_object_object_get(body, "continuation");
    if (!client->local_policy || !json_object_is_type(commands, json_type_array) ||
        json_object_array_length(commands) == 0 || json_object_array_length(commands) > 4 ||
        !json_object_is_type(token, json_type_string) || json_object_get_string_len(token) != 32) {
        if (body != NULL) {
            json_object_put(body);
        }
        return -1;
    }
    const char *continuation = json_object_get_string(token);
    if (strspn(continuation, "0123456789abcdef") != 32 || client->consumed_count >= 4) {
        json_object_put(body);
        return -1;
    }
    for (unsigned index = 0; index < client->consumed_count; ++index) {
        if (strcmp(client->consumed[index], continuation) == 0) {
            json_object_put(body);
            return -1;
        }
    }
    // 完整验证整轮提案，不能执行前一项后才发现后一项字段非法。
    for (size_t index = 0; index < json_object_array_length(commands); ++index) {
        json_object *entry = json_object_array_get_idx(commands, index);
        const char *fields[] = {"id", "command", "cwd"};
        const size_t limits[] = {128, 16384, 512};
        for (size_t field = 0; field < 3; ++field) {
            json_object *value = json_object_is_type(entry, json_type_object) ?
                json_object_object_get(entry, fields[field]) : NULL;
            if (!json_object_is_type(value, json_type_string) ||
                json_object_get_string_len(value) == 0 ||
                (size_t)json_object_get_string_len(value) > limits[field] ||
                strlen(json_object_get_string(value)) !=
                    (size_t)json_object_get_string_len(value)) {
                json_object_put(body);
                return -1;
            }
        }
        const char *id = json_object_get_string(json_object_object_get(entry, "id"));
        for (size_t previous = 0; previous < index; ++previous) {
            json_object *other = json_object_array_get_idx(commands, previous);
            if (strcmp(id, json_object_get_string(json_object_object_get(other, "id"))) == 0) {
                json_object_put(body);
                return -1;
            }
        }
    }
    strcpy(client->consumed[client->consumed_count++], continuation);
    LocalTask task = {
        .client = client,
        .id = response->request_id,
        .token = json_object_get_string(token),
        .live = true,
    };
    json_object *results = json_object_new_array();
    bool cancelled = false;
    bool stopped = false;
    for (size_t index = 0; index < json_object_array_length(commands); ++index) {
        json_object *entry = json_object_array_get_idx(commands, index);
        if (!json_object_is_type(entry, json_type_object)) {
            task.live = false;
            break;
        }
        json_object *id = json_object_object_get(entry, "id");
        json_object *command = json_object_object_get(entry, "command");
        json_object *cwd = json_object_object_get(entry, "cwd");
        if (!json_object_is_type(id, json_type_string) ||
            !json_object_is_type(command, json_type_string) ||
            !json_object_is_type(cwd, json_type_string) ||
            strlen(json_object_get_string(command)) !=
                (size_t)json_object_get_string_len(command) ||
            strlen(json_object_get_string(cwd)) != (size_t)json_object_get_string_len(cwd)) {
            task.live = false;
            break;
        }
        json_object *outcome;
        if (stopped) {
            outcome = json_object_new_object();
            json_object_object_add(outcome, "status", json_object_new_string("failed"));
            json_object_object_add(outcome, "effect", json_object_new_string("none"));
            json_object_object_add(outcome, "text", json_object_new_string("先前命令结果未知，未执行"));
        } else {
            outcome = local_command(json_object_get_string(command), client->workspace,
                json_object_get_string(cwd), client->observer, client->sandbox_socket,
                task_alive, task_connected, &task, &interrupted);
        }
        json_object_object_add(outcome, "id", json_object_get(id));
        json_object *code = json_object_object_get(outcome, "code");
        if (code != NULL && strcmp(json_object_get_string(code), "CONFIRMATION_REQUIRED") == 0) {
            client->approval_required = true;
        }
        const char *status = json_object_get_string(json_object_object_get(outcome, "status"));
        const char *effect = json_object_get_string(json_object_object_get(outcome, "effect"));
        fprintf(stderr, "本地执行结果：%s；实际影响：%s\n", status, effect);
        json_object_array_add(results, outcome);
        client->cleanup_failed = client->cleanup_failed ||
            json_object_get_boolean(json_object_object_get(outcome, "cleanup_failed"));
        if (strcmp(status, "cancelled") == 0 ||
            json_object_get_boolean(json_object_object_get(outcome, "cleanup_failed"))) {
            cancelled = true;
            break;
        }
        client->cleanup_failed = client->cleanup_failed ||
            json_object_get_boolean(json_object_object_get(outcome, "cleanup_failed"));
        stopped = strcmp(effect, "unknown") == 0 ||
            json_object_get_boolean(json_object_object_get(outcome, "cleanup_failed"));
    }
    json_object *submission = json_object_new_object();
    json_object_object_add(submission, "continuation", json_object_get(token));
    json_object_object_add(submission, "results", results);
    int result = task.live ? send_json(client,
        cancelled ? MESSAGE_TASK_CANCEL : MESSAGE_COMMAND_RESULT, response->request_id,
        submission) : -1;
    json_object_put(submission);
    json_object_put(body);
    return client->cleanup_failed ? -1 : result;
}

static int register_workspace(Client *client)
{
    json_object *body = json_object_new_object();
    const char *name = strrchr(client->workspace_name, '/');
    name = name == NULL ? client->workspace_name : name + 1;
    json_object_object_add(body, "name", json_object_new_string(name));
    uint64_t id = client->next_request_id++;
    int sent = send_json(client, MESSAGE_SESSION_OPEN, id, body);
    json_object_put(body);
    Frame response = {0};
    if (sent != 0 || receive_frame(client, &response) != 0) {
        return -1;
    }
    int status = response.request_id == id && response.type == MESSAGE_SESSION_READY ? 0 : -1;
    if (status != 0) {
        fputs("服务端未启用本地命令任务接口，未执行任何本地命令。\n", stderr);
    }
    frame_destroy(&response);
    return status;
}

static int client_request(Client *client, MessageType type, const char *query)
{
    if (client->cleanup_failed) {
        return -1;
    }
    client->consumed_count = 0;
    if (client->next_request_id == UINT64_MAX) {
        fprintf(stderr, "请求ID耗尽，请重新连接\n");
        return -1;
    }
    json_object *body = json_object_new_object();
    if (body == NULL) {
        return -1;
    }
    if (type != MESSAGE_PING) {
        json_object_object_add(body, "query", json_object_new_string(query));
    }
    const char *json = json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN);
    Frame request = {.type = type,
                     .request_id = client->next_request_id++,
                     .body = (unsigned char *)json,
                     .body_length = strlen(json)};
    unsigned char *encoded = NULL;
    size_t encoded_length = 0;
    if (frame_encode(&request, &encoded, &encoded_length) != 0) {
        json_object_put(body);
        fprintf(stderr, "无法编码请求帧\n");
        return -1;
    }
    int64_t deadline = deadline_after(client->timeout_ms);
    size_t offset = 0;
    while (offset < encoded_length) {
        if (wait_socket(client->socket_fd, POLLOUT, deadline, -1) != 0) {
            goto io_error;
        }
        const ssize_t sent =
            send(client->socket_fd, encoded + offset, encoded_length - offset, MSG_NOSIGNAL);
        if (sent < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            goto io_error;
        }
        if (sent == 0) {
            errno = EPIPE;
            goto io_error;
        }
        offset += (size_t)sent;
    }
    free(encoded);
    json_object_put(body);

    const bool show_wait = isatty(STDERR_FILENO) && isatty(STDOUT_FILENO) &&
                           type != MESSAGE_PING && getenv("TERM") != NULL &&
                           strcmp(getenv("TERM"), "dumb") != 0;
    int64_t request_started_ms = show_wait ? deadline - client->timeout_ms : -1;
    unsigned proposal_rounds = 0;
    unsigned char buffer[16384];
    while (true) {
        Frame response = {0};
        const char *decode_error = NULL;
        const int decoded = frame_decoder_next(&client->decoder, &response, &decode_error);
        if (decoded < 0) {
            fprintf(
                stderr, "服务端响应帧非法: %s\n", decode_error == NULL ? "未知错误" : decode_error);
            return -1;
        }
        if (decoded > 0) {
            if (response.request_id != request.request_id ||
                !frame_decoder_empty(&client->decoder)) {
                frame_destroy(&response);
                fprintf(stderr, "服务端响应ID或帧数量错误\n");
                return -1;
            }
            if (response.type == MESSAGE_COMMAND_PROPOSAL) {
                if (++proposal_rounds > 4 || execute_proposals(client, &response) != 0) {
                    frame_destroy(&response);
                    return -1;
                }
                frame_destroy(&response);
                deadline = deadline_after(client->timeout_ms);
                request_started_ms = show_wait ? deadline - client->timeout_ms : -1;
                continue;
            }
            const int displayed = display_response(&response, type);
            frame_destroy(&response);
            return displayed;
        }
        if (wait_socket(client->socket_fd, POLLIN, deadline, request_started_ms) != 0) {
            return -1;
        }
        const ssize_t received = recv(client->socket_fd, buffer, sizeof(buffer), 0);
        if (received < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            return -1;
        }
        if (received == 0) {
            fprintf(stderr, "服务端关闭连接或响应不完整\n");
            return -1;
        }
        if (frame_decoder_append(&client->decoder, buffer, (size_t)received) != 0) {
            fprintf(stderr, "服务端响应超过接收缓冲上限\n");
            return -1;
        }
    }

io_error:
    free(encoded);
    json_object_put(body);
    print_system_error("网络请求");
    return -1;
}

static void print_help(void)
{
    puts("/agent [问题]  Agent 自动选择可用工具\n"
         "/chat [问题]   模型对话，不调用工具\n"
         "/rag [问题]    检索 Linux 手册后回答，并显示来源\n"
         "/cmd [问题]    调用已启用的操作工具并解释结果\n"
         "不带问题时切换模式；带问题时仅发送本次请求。能力取决于服务端配置。\n"
         "/back 返回 Agent 模式；/help 帮助；/status 网络检查；/clear 清屏；/exit 退出\n"
         "/list 在 CMD 模式显示操作工具说明。");
}

static int repl(Client *client, const Options *options)
{
    puts("\n"
         "  ____      _    ____            _   \n"
         " |  _ \\    / \\  / ___| ___ _ __ | |_ \n"
         " | |_) |  / _ \\| |  _ / _ \\ '_ \\| __|\n"
         " |  _ <  / ___ \\ |_| |  __/ | | | |_ \n"
         " |_| \\_\\/_/   \\_\\____|\\___|_| |_|\\__|\n");
    printf("连接地址 %s:%u，默认使用 Agent 模式；输入 /help 查看帮助。\n",
           options->host,
           options->port);
    ClientMode mode = CLIENT_MODE_AGENT;
    char *line = NULL;
    size_t capacity = 0;
    while (!interrupted) {
        printf("%s> ", mode_name(mode));
        fflush(stdout);
        const ssize_t length = getline(&line, &capacity, stdin);
        if (length < 0) {
            break;
        }
        size_t input_start = 0;
        size_t input_end = (size_t)length;
        while (input_start < input_end && strchr(" \t\r\n", line[input_start]) != NULL) {
            ++input_start;
        }
        while (input_end > input_start && strchr(" \t\r\n", line[input_end - 1]) != NULL) {
            --input_end;
        }
        if (input_start == input_end) {
            continue;
        }
        if (line[input_start] != '/') {
            char *query =
                strndup(line, (size_t)length - (line[(size_t)length - 1] == '\n' ? 1U : 0U));
            if (query == NULL) {
                break;
            }
            const int result = client_request(client, mode_message_type(mode), query);
            free(query);
            if (result < 0) {
                free(line);
                return -1;
            }
            continue;
        }

        const size_t command_start = input_start;
        size_t command_end = command_start;
        while (command_end < input_end && line[command_end] != ' ' && line[command_end] != '\t') {
            ++command_end;
        }
        size_t argument_start = command_end;
        while (argument_start < input_end &&
               (line[argument_start] == ' ' || line[argument_start] == '\t')) {
            ++argument_start;
        }
        const char *argument = line + argument_start;
        const size_t argument_length = input_end - argument_start;
        const size_t command_length = command_end - command_start;
        struct {
            const char *name;
            ClientMode mode;
        } commands[] = {
            {"/agent", CLIENT_MODE_AGENT},
            {"/chat", CLIENT_MODE_CHAT},
            {"/rag", CLIENT_MODE_RAG},
            {"/cmd", CLIENT_MODE_CMD},
        };
        bool mode_command = false;
        for (size_t index = 0; index < sizeof(commands) / sizeof(commands[0]); ++index) {
            if (strlen(commands[index].name) != command_length ||
                memcmp(line + command_start, commands[index].name, command_length) != 0) {
                continue;
            }
            mode_command = true;
            if (argument_length == 0) {
                mode = commands[index].mode;
            } else {
                char *query = strndup(argument, argument_length);
                if (query == NULL) {
                    free(line);
                    return -1;
                }
                const int result =
                    client_request(client, mode_message_type(commands[index].mode), query);
                free(query);
                if (result < 0) {
                    free(line);
                    return -1;
                }
            }
            break;
        }
        if (mode_command) {
            continue;
        }
        if (argument_length > 0) {
            puts("此命令不接受参数。");
            continue;
        }
        const size_t command_length_exit = strlen("/exit");
        if (command_length == command_length_exit &&
            memcmp(line + command_start, "/exit", command_length) == 0) {
            break;
        }
        if (command_length == strlen("/back") &&
            memcmp(line + command_start, "/back", command_length) == 0) {
            mode = CLIENT_MODE_AGENT;
        } else if ((command_length == strlen("/help") &&
                    memcmp(line + command_start, "/help", command_length) == 0) ||
                   (command_length == 1 && line[command_start] == '/')) {
            print_help();
        } else if (command_length == strlen("/clear") &&
                   memcmp(line + command_start, "/clear", command_length) == 0) {
            fputs("\033[2J\033[H", stdout);
        } else if (command_length == strlen("/status") &&
                   memcmp(line + command_start, "/status", command_length) == 0) {
            if (client_request(client, MESSAGE_PING, "") < 0) {
                free(line);
                return -1;
            }
        } else if (command_length == strlen("/list") &&
                   memcmp(line + command_start, "/list", command_length) == 0) {
            puts(mode == CLIENT_MODE_CMD ?
                 "命令工具：run_command(command, cwd)。工作区为本客户端启动目录。\n"
                 "只读命令直接执行；写操作须确认后在容器中执行，并回写当前工作区。\n"
                 "网络与凭证按单条命令单独授权；拒绝即结束整个任务。" : "请先进入 /cmd 模式。");
        } else {
            puts("未知命令，输入 /help 查看帮助。");
        }
    }
    free(line);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc == 2 && strcmp(argv[1], "--help") == 0) {
        puts("agent [--host IPv4] [--port PORT] [--timeout-ms N]\n"
             "      [--mode agent|chat|rag|cmd --query TEXT] [--remote-only]\n"
             "响应等待默认 130000 毫秒，可通过 --timeout-ms 调整。");
        return 0;
    }
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = interrupt_handler;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) != 0) {
        print_system_error("sigaction");
        return 1;
    }
    setvbuf(stdin, NULL, _IONBF, 0);
    Options options;
    if (parse_options(argc, argv, &options) != 0) {
        return 2;
    }
    Client client;
    if (client_init(&client, &options) != 0) {
        print_system_error("连接Agent失败");
        client_destroy(&client);
        return 1;
    }
    if (options.local_policy && register_workspace(&client) != 0) {
        client_destroy(&client);
        return 1;
    }
    int result;
    if (options.once) {
        result = client_request(&client, mode_message_type(options.mode), options.query);
        if (result > 0) {
            result = 3;
        } else if (result < 0) {
            result = 1;
        }
    } else {
        result = repl(&client, &options) == 0 ? 0 : 1;
    }
    if (client.approval_required) {
        result = 3;
    }
    client_destroy(&client);
    if (interrupted && !options.once) {
        return 0;
    }
    return result;
}
