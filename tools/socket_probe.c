#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <json-c/json.h>
#include <poll.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

/* 独立生成线上帧，避免与服务端共用编解码而掩盖协议问题。 */
typedef struct {
    int fd;
    bool connecting;
    bool done;
    uint64_t identity;
    unsigned kind;
    char code[64];
    unsigned char *request;
    size_t request_length;
    size_t sent;
    unsigned char header[22];
    size_t received;
    unsigned char *body;
    size_t length;
    double started;
    double connected;
    double sent_at;
    double finished;
} Peer;

/* 配置在创建线程前冻结；业务压测复用同一组问题，不缓存响应。 */
static bool business_mode;
static char *business_bodies[8];
static unsigned business_kinds[8];
static size_t business_count;

static double now(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        return -1;
    }
    return value.tv_sec + value.tv_nsec / 1e9;
}

static json_object *parse_object(const char *text, size_t length)
{
    struct json_tokener *tokener = json_tokener_new();
    if (tokener == NULL) {
        return NULL;
    }
    json_tokener_set_flags(tokener, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    json_object *value = json_tokener_parse_ex(tokener, text, (int)length);
    bool valid = json_tokener_get_error(tokener) == json_tokener_success &&
                 json_tokener_get_parse_end(tokener) == length &&
                 json_object_is_type(value, json_type_object);
    json_tokener_free(tokener);
    if (!valid) {
        json_object_put(value);
        return NULL;
    }
    return value;
}

static int load_business(json_object *settings)
{
    json_object *requests = NULL;
    if (!json_object_object_get_ex(settings, "requests", &requests) ||
        !json_object_is_type(requests, json_type_array)) {
        return -1;
    }
    business_count = json_object_array_length(requests);
    if (business_count == 0 || business_count > 8) {
        business_count = 0;
        return -1;
    }
    for (size_t i = 0; i < business_count; ++i) {
        json_object *entry = json_object_array_get_idx(requests, i);
        json_object *query = NULL;
        json_object *kind = NULL;
        if (!json_object_is_type(entry, json_type_object) ||
            !json_object_object_get_ex(entry, "query", &query) ||
            !json_object_is_type(query, json_type_string) ||
            json_object_get_string_len(query) == 0 || json_object_get_string_len(query) > 4096 ||
            !json_object_object_get_ex(entry, "kind", &kind) ||
            !json_object_is_type(kind, json_type_int) ||
            (json_object_get_int(kind) != 2 && json_object_get_int(kind) != 4)) {
            return -1;
        }
        json_object *body = json_object_new_object();
        if (body == NULL) {
            return -1;
        }
        json_object_object_add(body, "query", json_object_get(query));
        business_bodies[i] = strdup(json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN));
        business_kinds[i] = (unsigned)json_object_get_int(kind);
        json_object_put(body);
        if (business_bodies[i] == NULL) {
            return -1;
        }
    }
    return 0;
}

static uint64_t number(const unsigned char *bytes, size_t length);

static bool business_response(Peer *peer, json_object *body)
{
    json_object *value = NULL;
    if (number(peer->header + 6, 2) == 101) {
        if (!json_object_object_get_ex(body, "code", &value) ||
            !json_object_is_type(value, json_type_string) ||
            json_object_get_string_len(value) >= (int)sizeof(peer->code)) {
            return false;
        }
        const char *code = json_object_get_string(value);
        if (strcmp(code, "INVALID_REQUEST") != 0 && strcmp(code, "SERVER_BUSY") != 0 &&
            strcmp(code, "UPSTREAM_TIMEOUT") != 0 && strcmp(code, "UPSTREAM_UNAVAILABLE") != 0 &&
            strcmp(code, "INTERNAL_ERROR") != 0) {
            return false;
        }
        snprintf(peer->code, sizeof(peer->code), "%s", code);
        return true;
    }
    json_object *text = NULL;
    json_object *sources = NULL;
    if (!json_object_object_get_ex(body, "text", &text) ||
        !json_object_is_type(text, json_type_string) || json_object_get_string_len(text) == 0 ||
        !json_object_object_get_ex(body, "sources", &sources) ||
        !json_object_is_type(sources, json_type_array)) {
        return false;
    }
    snprintf(peer->code, sizeof(peer->code), "%s",
             json_object_array_length(sources) == 0 ? "NO_EVIDENCE" : "OK");
    if (json_object_object_get_ex(body, "actions", &value)) {
        if (!json_object_is_type(value, json_type_array)) {
            return false;
        }
        for (size_t i = 0; i < json_object_array_length(value); ++i) {
            json_object *action = json_object_array_get_idx(value, i);
            json_object *status = NULL;
            if (!json_object_object_get_ex(action, "status", &status) ||
                !json_object_is_type(status, json_type_string)) {
                return false;
            }
            if (strcmp(json_object_get_string(status), "success") != 0) {
                strcpy(peer->code, "PARTIAL");
            }
        }
    }
    const char *message = json_object_get_string(text);
    if (strncmp(message, "请求超时", strlen("请求超时")) == 0 ||
        strncmp(message, "达到工具", strlen("达到工具")) == 0) {
        strcpy(peer->code, "PARTIAL");
    }
    return true;
}

static uint64_t number(const unsigned char *bytes, size_t length)
{
    uint64_t value = 0;
    for (size_t i = 0; i < length; ++i) {
        value = (value << 8) | bytes[i];
    }
    return value;
}

static int compare(const void *a, const void *b)
{
    double left = *(const double *)a;
    double right = *(const double *)b;
    return (left > right) - (left < right);
}

static double p95(double *values, size_t count)
{
    if (count == 0) {
        return 0;
    }
    qsort(values, count, sizeof(*values), compare);
    return values[(95 * count + 99) / 100 - 1];
}

static int prepare(Peer *peer, unsigned kind, uint64_t identity, const char *body)
{
    size_t length = strlen(body);
    free(peer->request);
    free(peer->body);
    peer->body = NULL;
    peer->request = malloc(20 + length);
    if (peer->request == NULL) {
        return -1;
    }
    peer->request_length = 20 + length;
    peer->sent = 0;
    peer->received = 0;
    peer->length = 0;
    peer->done = false;
    peer->kind = kind;
    peer->identity = identity;
    memcpy(peer->request, "RAGT\0\2", 6);
    peer->request[6] = (unsigned char)(kind >> 8);
    peer->request[7] = (unsigned char)kind;
    uint32_t size = htonl((uint32_t)length);
    memcpy(peer->request + 8, &size, 4);
    for (size_t i = 0; i < 8; ++i) {
        peer->request[19 - i] = (unsigned char)(identity >> (8 * i));
    }
    memcpy(peer->request + 20, body, length);
    peer->started = now();
    return peer->started < 0 ? -1 : 0;
}

static int connect_peer(Peer *peer, unsigned port)
{
    peer->fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (peer->fd < 0) {
        return -1;
    }
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    if (connect(peer->fd, (struct sockaddr *)&address, sizeof(address)) != 0 &&
        errno != EINPROGRESS) {
        return -1;
    }
    peer->connecting = true;
    return 0;
}

static int finish_connect(Peer *peer)
{
    int error = 0;
    socklen_t size = sizeof(error);
    if (getsockopt(peer->fd, SOL_SOCKET, SO_ERROR, &error, &size) != 0 || error != 0) {
        return -1;
    }
    peer->connecting = false;
    peer->connected = now();
    return peer->connected < 0 ? -1 : 0;
}

/* 返回0表示暂不可读，EOF与不可恢复错误返回-1。 */
static ssize_t receive_bytes(int fd, unsigned char *destination, size_t length)
{
    ssize_t count;
    do {
        count = recv(fd, destination, length, 0);
    } while (count < 0 && errno == EINTR);
    if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        return 0;
    }
    if (count == 0) {
        errno = ECONNRESET;
        return -1;
    }
    return count;
}

static int advance(Peer *peer, short events)
{
    if ((events & POLLNVAL) != 0) {
        return -1;
    }
    if (peer->connecting && finish_connect(peer) != 0) {
        return -1;
    }
    while (peer->sent < peer->request_length) {
        ssize_t count = send(peer->fd, peer->request + peer->sent,
                             peer->request_length - peer->sent, MSG_NOSIGNAL);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return 0;
        }
        if (count <= 0) {
            return -1;
        }
        peer->sent += (size_t)count;
        peer->sent_at = now();
        if (peer->sent_at < 0) {
            return -1;
        }
    }
    /* 不在仅可写事件上重复recv，避免负载发生器自己制造无效系统调用。 */
    if ((events & (POLLIN | POLLERR | POLLHUP)) == 0) {
        return 0;
    }
    if (peer->kind == 200) {
        while (peer->received < 22) {
            ssize_t count = receive_bytes(peer->fd, peer->header + peer->received,
                                          22 - peer->received);
            if (count <= 0) {
                return (int)count;
            }
            peer->received += (size_t)count;
        }
        if (memcmp(peer->header, "RAGT\0\2", 6) != 0 ||
            number(peer->header + 6, 2) != 201 || number(peer->header + 8, 4) != 2 ||
            number(peer->header + 12, 8) != peer->identity ||
            memcmp(peer->header + 20, "{}", 2) != 0) {
            return -1;
        }
        peer->done = true;
        peer->finished = now();
        return peer->finished < 0 ? -1 : 0;
    }
    while (!peer->done) {
        bool header = peer->received < 20;
        size_t offset = header ? peer->received : peer->received - 20;
        size_t wanted = header ? 20 - offset : peer->length - offset;
        unsigned char *destination = header ? peer->header + offset : peer->body + offset;
        ssize_t count = receive_bytes(peer->fd, destination, wanted);
        if (count <= 0) {
            return (int)count;
        }
        peer->received += (size_t)count;
        if (header && peer->received == 20) {
            if (memcmp(peer->header, "RAGT\0\2", 6) != 0 ||
                number(peer->header + 12, 8) != peer->identity ||
                (number(peer->header + 6, 2) != 101U &&
                 (!business_mode || number(peer->header + 6, 2) != 100U))) {
                return -1;
            }
            peer->length = (size_t)number(peer->header + 8, 4);
            if (peer->length == 0 || peer->length > 1048576) {
                return -1;
            }
            peer->body = calloc(peer->length + 1, 1);
            if (peer->body == NULL) {
                return -1;
            }
        }
        if (peer->received == 20 + peer->length) {
            json_object *body = parse_object((char *)peer->body, peer->length);
            json_object *code = NULL;
            bool valid;
            if (business_mode) {
                valid = body != NULL && business_response(peer, body);
            } else {
                valid = body != NULL && json_object_object_get_ex(body, "code", &code) &&
                        json_object_is_type(code, json_type_string) &&
                        strcmp(json_object_get_string(code), "SERVER_BUSY") == 0;
            }
            json_object_put(body);
            if (!valid) {
                return -1;
            }
            peer->done = true;
            peer->finished = now();
            if (peer->finished < 0) {
                return -1;
            }
        }
    }
    return 0;
}

static int run(unsigned port, size_t count, bool overload, const char *body, json_object *report)
{
    Peer *peers = calloc(count + 1, sizeof(*peers));
    struct pollfd *fds = calloc(count + 1, sizeof(*fds));
    double *durations = calloc(count * 3, sizeof(*durations));
    int status = -1;
    size_t samples = 0;
    double business_started = now();
    if (peers != NULL) {
        for (size_t i = 0; i <= count; ++i) {
            peers[i].fd = -1;
        }
    }
    if (peers == NULL || fds == NULL || durations == NULL) {
        goto cleanup;
    }
    for (size_t round = 0; round < ((overload || business_mode) ? 1U : 3U); ++round) {
        double started = now();
        if (started < 0) {
            goto cleanup;
        }
        size_t total = count;
        bool ping_added = false;
        for (size_t i = 0; i < count; ++i) {
            unsigned kind = 200;
            if (business_mode) {
                kind = business_kinds[i % business_count];
            } else if (overload) {
                kind = 2;
            }
            const char *payload = business_mode ? business_bodies[i % business_count] : body;
            uint64_t identity = (overload || business_mode) ? i + 1 : round + 1;
            if (prepare(&peers[i], kind, identity, payload) != 0) {
                goto cleanup;
            }
            if (round == 0 && connect_peer(&peers[i], port) != 0) {
                if (!business_mode) {
                    goto cleanup;
                }
                strcpy(peers[i].code, "CLIENT_ERROR");
                peers[i].done = true;
                peers[i].finished = now();
            }
        }
        /* PING验收先完成建连，与Streams客户端的空闲连接场景保持一致。 */
        if (!overload && !business_mode && round == 0) {
            size_t connected = 0;
            while (connected < count) {
                if (now() - started > 30) {
                    goto cleanup;
                }
                for (size_t i = 0; i < count; ++i) {
                    fds[i].fd = peers[i].connecting ? peers[i].fd : -1;
                    fds[i].events = POLLOUT;
                }
                int ready = poll(fds, count, 20);
                if (ready < 0 && errno == EINTR) {
                    continue;
                }
                if (ready < 0) {
                    goto cleanup;
                }
                for (size_t i = 0; i < count; ++i) {
                    if (fds[i].revents != 0) {
                        if (finish_connect(&peers[i]) != 0) {
                            goto cleanup;
                        }
                        ++connected;
                    }
                }
            }
            struct timespec pause = {.tv_sec = 2, .tv_nsec = 0};
            while (nanosleep(&pause, &pause) != 0) {
                if (errno != EINTR) {
                    goto cleanup;
                }
            }
        }
        if (!overload && !business_mode) {
            started = now();
            for (size_t i = 0; i < count; ++i) {
                peers[i].started = started;
            }
        }
        while (true) {
            double current = now();
            if (current < 0) {
                goto cleanup;
            }
            double elapsed = current - started;
            if (business_mode && elapsed > 130) {
                for (size_t i = 0; i < count; ++i) {
                    if (!peers[i].done) {
                        peers[i].done = true;
                        peers[i].finished = current;
                        strcpy(peers[i].code, "CLIENT_TIMEOUT");
                    }
                }
                break;
            }
            if (!business_mode && elapsed > (overload ? 8 : 10)) {
                fprintf(stderr, "探测超过时限\n");
                goto cleanup;
            }
            if (overload && elapsed >= 4 && !ping_added) {
                if (prepare(&peers[count], 200, 999999, "{}") != 0 ||
                    connect_peer(&peers[count], port) != 0) {
                    goto cleanup;
                }
                total = count + 1;
                ping_added = true;
            }
            size_t done = 0;
            for (size_t i = 0; i < total; ++i) {
                fds[i].fd = peers[i].done ? -1 : peers[i].fd;
                fds[i].events = peers[i].connecting || peers[i].sent < peers[i].request_length
                                    ? POLLOUT : POLLIN;
                done += peers[i].done;
            }
            if ((!overload && done == count) || (ping_added && peers[count].done)) {
                break;
            }
            int ready = poll(fds, total, 20);
            if (ready < 0 && errno == EINTR) {
                continue;
            }
            if (ready < 0) {
                goto cleanup;
            }
            for (size_t i = 0; i < total; ++i) {
                if (fds[i].revents != 0 && advance(&peers[i], fds[i].revents) != 0) {
                    fprintf(stderr, "连接%zu响应或系统调用失败\n", i);
                    if (!business_mode) {
                        goto cleanup;
                    }
                    peers[i].done = true;
                    peers[i].finished = now();
                    strcpy(peers[i].code, "CLIENT_ERROR");
                }
            }
        }
        for (size_t i = 0; i < count; ++i) {
            if (peers[i].done) {
                durations[samples++] = peers[i].finished - peers[i].started;
            }
        }
    }
    json_object_object_add(report, "client_io", json_object_new_string("native-poll"));
    if (business_mode) {
        json_object *rows = json_object_new_array();
        for (size_t i = 0; i < count; ++i) {
            json_object *row = json_object_new_object();
            json_object_object_add(row, "id", json_object_new_int64((int64_t)peers[i].identity));
            json_object_object_add(row, "code", json_object_new_string(peers[i].code));
            json_object_object_add(row, "mode",
                                   json_object_new_string(peers[i].kind == 2 ? "rag" : "agent"));
            json_object_object_add(row, "started_s", json_object_new_double(peers[i].started));
            json_object_object_add(row, "sent_s", json_object_new_double(peers[i].sent_at));
            json_object_object_add(row, "finished_s", json_object_new_double(peers[i].finished));
            json_object_object_add(row, "seconds",
                                   json_object_new_double(peers[i].finished - peers[i].started));
            json_object_object_add(row, "response_bytes",
                                   json_object_new_int64((int64_t)peers[i].length));
            json_object_array_add(rows, row);
        }
        json_object_object_add(report, "rows", rows);
        json_object_object_add(report, "elapsed_s",
                               json_object_new_double(now() - business_started));
    } else if (overload) {
        json_object *rows = json_object_new_array();
        for (size_t i = 0; i < count; ++i) {
            if (peers[i].done) {
                json_object *row = json_object_new_object();
                json_object_object_add(row, "seconds",
                                       json_object_new_double(
                                           peers[i].finished - peers[i].started));
                json_object_object_add(row, "connect_s",
                                       json_object_new_double(
                                           peers[i].connected - peers[i].started));
                json_object_object_add(row, "reply_s",
                                       json_object_new_double(
                                           peers[i].finished - peers[i].sent_at));
                json_object_array_add(rows, row);
            }
        }
        json_object_object_add(report, "rows", rows);
        json_object_object_add(report, "clients", json_object_new_int64((int64_t)count));
        json_object_object_add(report, "preconnected", json_object_new_boolean(false));
        json_object_object_add(report, "rejected", json_object_new_int64((int64_t)samples));
        json_object_object_add(report, "reject_p95_s",
                                   json_object_new_double(p95(durations, samples)));
        size_t collected = 0;
        for (size_t i = 0; i < count; ++i) {
            if (peers[i].done) {
                durations[collected++] = peers[i].connected - peers[i].started;
            }
        }
        json_object_object_add(report, "reject_connect_p95_s",
                               json_object_new_double(p95(durations, collected)));
        collected = 0;
        for (size_t i = 0; i < count; ++i) {
            if (peers[i].done) {
                durations[collected++] = peers[i].finished - peers[i].sent_at;
            }
        }
        json_object_object_add(report, "reject_reply_p95_s",
                               json_object_new_double(p95(durations, collected)));
        json_object_object_add(report, "ping_ok", json_object_new_boolean(true));
        json_object_object_add(report, "ping_seconds",
                               json_object_new_double(
                                       peers[count].finished - peers[count].started));
        json_object_object_add(report, "abandoned_after_probe",
                               json_object_new_int64((int64_t)(count - samples)));
    } else {
        json_object_object_add(report, "connections", json_object_new_int64((int64_t)count));
        json_object_object_add(report, "ping_success", json_object_new_int64((int64_t)samples));
        json_object_object_add(report, "ping_p95_s",
                                   json_object_new_double(p95(durations, samples)));
    }
    status = 0;
cleanup:
    if (peers != NULL) {
        for (size_t i = 0; i <= count; ++i) {
            if (peers[i].fd >= 0) {
                close(peers[i].fd);
            }
            free(peers[i].request);
            free(peers[i].body);
        }
    }
    free(peers);
    free(fds);
    free(durations);
    return status;
}

enum {
    PROBE_THREADS = 2
};

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t ready;
    bool started;
    bool aborted;
} StartGate;

typedef struct {
    StartGate *gate;
    unsigned port;
    size_t count;
    const char *body;
    json_object *report;
    int status;
} ProbeJob;

static void *probe_worker(void *argument)
{
    ProbeJob *job = argument;
    pthread_mutex_lock(&job->gate->mutex);
    while (!job->gate->started) {
        pthread_cond_wait(&job->gate->ready, &job->gate->mutex);
    }
    bool aborted = job->gate->aborted;
    pthread_mutex_unlock(&job->gate->mutex);
    if (!aborted) {
        job->status = run(job->port, job->count, true, job->body, job->report);
    }
    return NULL;
}

/* 每个线程独占自己的Socket和结果；主线程只在join后汇总原始样本。 */
static int run_parallel(unsigned port, size_t count, const char *body, json_object *report)
{
    StartGate gate = {
        .mutex = PTHREAD_MUTEX_INITIALIZER,
        .ready = PTHREAD_COND_INITIALIZER,
        .started = false,
        .aborted = false,
    };
    ProbeJob jobs[PROBE_THREADS] = {0};
    pthread_t threads[PROBE_THREADS];
    size_t created = 0;
    for (size_t i = 0; i < PROBE_THREADS; ++i) {
        jobs[i].gate = &gate;
        jobs[i].port = port;
        jobs[i].count = count / PROBE_THREADS + (i < count % PROBE_THREADS ? 1U : 0U);
        jobs[i].body = body;
        jobs[i].report = json_object_new_object();
        jobs[i].status = -1;
        if (jobs[i].report == NULL ||
            pthread_create(&threads[i], NULL, probe_worker, &jobs[i]) != 0) {
            break;
        }
        ++created;
    }
    pthread_mutex_lock(&gate.mutex);
    gate.aborted = created != PROBE_THREADS;
    gate.started = true;
    pthread_cond_broadcast(&gate.ready);
    pthread_mutex_unlock(&gate.mutex);
    int status = created == PROBE_THREADS ? 0 : -1;
    for (size_t i = 0; i < created; ++i) {
        int joined = pthread_join(threads[i], NULL);
        if (joined != 0) {
            fprintf(stderr, "回收负载线程失败: %s\n", strerror(joined));
            /* 无法确认线程停止时终止工具进程，不能释放线程仍可能持有的内存。 */
            exit(EXIT_FAILURE);
        }
        if (jobs[i].status != 0) {
            status = -1;
        }
    }
    double *values = calloc(count * 3, sizeof(*values));
    size_t samples = 0;
    double ping = 0;
    json_object *rows = json_object_new_array();
    if (values == NULL || rows == NULL) {
        status = -1;
    }
    if (status == 0) {
        for (size_t i = 0; i < PROBE_THREADS; ++i) {
            json_object *measurements = NULL;
            json_object *ping_value = NULL;
            json_object_object_get_ex(jobs[i].report, "rows", &measurements);
            json_object_object_get_ex(jobs[i].report, "ping_seconds", &ping_value);
            double current_ping = json_object_get_double(ping_value);
            if (current_ping > ping) {
                ping = current_ping;
            }
            for (size_t j = 0; j < json_object_array_length(measurements); ++j) {
                json_object *row = json_object_array_get_idx(measurements, j);
                json_object *value = NULL;
                json_object_object_get_ex(row, "seconds", &value);
                values[samples] = json_object_get_double(value);
                json_object_object_get_ex(row, "connect_s", &value);
                values[count + samples] = json_object_get_double(value);
                json_object_object_get_ex(row, "reply_s", &value);
                values[2 * count + samples] = json_object_get_double(value);
                json_object_array_add(rows, json_object_get(row));
                ++samples;
            }
        }
        if (samples == 0) {
            status = -1;
        } else {
            json_object_object_add(report, "client_io", json_object_new_string("native-poll"));
            json_object_object_add(report, "client_threads", json_object_new_int(PROBE_THREADS));
            json_object_object_add(report, "clients", json_object_new_int64((int64_t)count));
            json_object_object_add(report, "preconnected", json_object_new_boolean(false));
            json_object_object_add(report, "rejected", json_object_new_int64((int64_t)samples));
            json_object_object_add(report, "reject_p95_s",
                                   json_object_new_double(p95(values, samples)));
            json_object_object_add(report, "reject_connect_p95_s",
                                   json_object_new_double(p95(values + count, samples)));
            json_object_object_add(report, "reject_reply_p95_s",
                                   json_object_new_double(p95(values + 2 * count, samples)));
            json_object_object_add(report, "ping_ok", json_object_new_boolean(true));
            json_object_object_add(report, "ping_probes", json_object_new_int(PROBE_THREADS));
            json_object_object_add(report, "ping_seconds", json_object_new_double(ping));
            json_object_object_add(report, "abandoned_after_probe",
                                   json_object_new_int64((int64_t)(count - samples)));
            json_object_object_add(report, "rows", rows);
            rows = NULL;
        }
    }
    json_object_put(rows);
    free(values);
    for (size_t i = 0; i < PROBE_THREADS; ++i) {
        json_object_put(jobs[i].report);
    }
    pthread_cond_destroy(&gate.ready);
    pthread_mutex_destroy(&gate.mutex);
    return status;
}

int main(void)
{
    char input[32768];
    size_t length = fread(input, 1, sizeof(input) - 1, stdin);
    input[length] = '\0';
    json_object *settings = parse_object(input, length);
    json_object *port = NULL;
    json_object *count = NULL;
    json_object *query = NULL;
    json_object *mode = NULL;
    if (ferror(stdin) || !feof(stdin) || settings == NULL ||
        !json_object_object_get_ex(settings, "port", &port) ||
        !json_object_object_get_ex(settings, "count", &count) ||
        !json_object_object_get_ex(settings, "mode", &mode) ||
        !json_object_is_type(port, json_type_int) || !json_object_is_type(count, json_type_int) ||
        !json_object_is_type(mode, json_type_string) || json_object_get_int(port) < 1 ||
        json_object_get_int(port) > 65535 || json_object_get_int(count) < 1 ||
        json_object_get_int(count) > 2048) {
        json_object_put(settings);
        fprintf(stderr, "探测配置非法\n");
        return 1;
    }
    bool overload = strcmp(json_object_get_string(mode), "overload") == 0;
    business_mode = strcmp(json_object_get_string(mode), "business") == 0;
    if ((!overload && !business_mode && strcmp(json_object_get_string(mode), "ping") != 0) ||
        (overload && (!json_object_object_get_ex(settings, "query", &query) ||
                      !json_object_is_type(query, json_type_string)))) {
        json_object_put(settings);
        return 1;
    }
    if (business_mode && load_business(settings) != 0) {
        for (size_t i = 0; i < business_count; ++i) {
            free(business_bodies[i]);
        }
        json_object_put(settings);
        return 1;
    }
    json_object *request = json_object_new_object();
    if (overload) {
        json_object_object_add(request, "query", json_object_get(query));
    }
    json_object *report = json_object_new_object();
    unsigned target_port = (unsigned)json_object_get_int(port);
    size_t target_count = (size_t)json_object_get_int(count);
    const char *body = json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN);
    int result;
    if (overload && target_count >= PROBE_THREADS) {
        result = run_parallel(target_port, target_count, body, report);
    } else {
        result = run(target_port, target_count, overload, body, report);
    }
    if (result == 0) {
        struct rusage usage;
        if (getrusage(RUSAGE_SELF, &usage) != 0) {
            result = -1;
        } else {
            double cpu = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6 +
                         usage.ru_stime.tv_sec + usage.ru_stime.tv_usec / 1e6;
            json_object_object_add(report, "client_cpu_s", json_object_new_double(cpu));
        }
    }
    if (result == 0) {
        puts(json_object_to_json_string_ext(report, JSON_C_TO_STRING_PLAIN));
    }
    for (size_t i = 0; i < business_count; ++i) {
        free(business_bodies[i]);
    }
    json_object_put(report);
    json_object_put(request);
    json_object_put(settings);
    return result == 0 ? 0 : 1;
}
