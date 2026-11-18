#define _POSIX_C_SOURCE 200809L
#include "server/agent_http.h"
#include "server/worker_pool.h"
#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static char endpoint[1024];
static unsigned timeout = 12000;
static bool tasks_enabled;
static char session_server_id[33];
typedef struct {
    unsigned char *data;
    size_t length;
} Buffer;

int agent_http_configure(const char *url, unsigned timeout_ms)
{
    if (strlen(url) >= sizeof(endpoint)) {
        return -1;
    }
    session_server_id[0] = '\0';
    strcpy(endpoint, url);
    size_t length = strlen(url);
    tasks_enabled = length >= 6 && strcmp(url + length - 6, "/tasks") == 0;
    timeout = timeout_ms;
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK ? 0 : -1;
}

bool agent_http_tasks_enabled(void)
{
    return tasks_enabled;
}

static size_t receive_body(char *data, size_t size, size_t count, void *context)
{
    Buffer *buffer = context;
    size_t length = size * count;
    if (length > RAGENT_MAX_BODY_SIZE - buffer->length) {
        return 0;
    }
    unsigned char *next = realloc(buffer->data, buffer->length + length + 1);
    if (!next) {
        return 0;
    }
    buffer->data = next;
    memcpy(next + buffer->length, data, length);
    buffer->length += length;
    next[buffer->length] = 0;
    return length;
}

static int transfer_progress(void *context, curl_off_t download_total, curl_off_t downloaded,
                              curl_off_t upload_total, curl_off_t uploaded)
{
    (void)download_total;
    (void)downloaded;
    (void)upload_total;
    (void)uploaded;
    return worker_lifetime_cancelled(context) ? 1 : 0;
}

int agent_http_request(const Frame *request, unsigned remaining_ms, Frame *response)
{
    return agent_http_request_controlled(request, remaining_ms, NULL, 0, response);
}

int agent_http_request_controlled(const Frame *request, unsigned remaining_ms,
                                  const WorkerLifetime *lifetime, uint64_t connection_id,
                                  Frame *response)
{
    if (remaining_ms == 0) {
        return frame_make_error(request->request_id, "UPSTREAM_TIMEOUT", "请求时限已到", response);
    }
    struct timespec started;
    if (clock_gettime(CLOCK_MONOTONIC, &started) != 0) {
        return frame_make_error(request->request_id, "INTERNAL_ERROR", "无法读取请求时钟", response);
    }
    if (!endpoint[0]) {
        return handle_fixed_request(request, response);
    }
    /* 复用现有请求校验，避免把非法请求发送给上游。 */
    Frame checked = {0};
    if (handle_fixed_request(request, &checked) != 0) {
        return -1;
    }
    if (checked.type == MESSAGE_ERROR) {
        *response = checked;
        return 0;
    }
    frame_destroy(&checked);
    json_object *body = json_parse_object(request->body, request->body_length);
    if (!body) {
        return -1;
    }
    const char *modes[] = {"", "chat", "rag", "cmd", "agent"};
    char id[32];
    snprintf(id, sizeof(id), "%llu", (unsigned long long)request->request_id);
    json_object_object_add(body, "request_id", json_object_new_string(id));
    json_object_object_add(body, "mode", json_object_new_string(modes[request->type]));
    // 外部TCP扩展字段不能选择或伪造工作区身份。
    json_object_object_del(body, "session");
    if (session_server_id[0] != '\0' && connection_id >= 3) {
        char connection[32];
        snprintf(connection, sizeof(connection), "%llu", (unsigned long long)connection_id);
        json_object *session = json_object_new_object();
        json_object_object_add(session, "server_id", json_object_new_string(session_server_id));
        json_object_object_add(session, "connection_id", json_object_new_string(connection));
        json_object_object_add(body, "session", session);
    }
    unsigned budget_ms = remaining_ms < timeout ? remaining_ms : timeout;
    if (tasks_enabled && (request->type == MESSAGE_CMD_REQUEST ||
                          request->type == MESSAGE_AGENT_REQUEST)) {
        json_object *deadline_value = json_object_object_get(body, "private_deadline_ms");
        int64_t deadline = json_object_get_int64(deadline_value);
        int64_t left = deadline - ((int64_t)started.tv_sec * 1000 + started.tv_nsec / 1000000);
        if (left <= 0) {
            json_object_put(body);
            return frame_make_error(request->request_id, "UPSTREAM_TIMEOUT",
                                    "任务累计计算时限已到", response);
        }
        if ((uint64_t)left < budget_ms) {
            budget_ms = (unsigned)left;
        }
    }
    json_object_object_del(body, "private_deadline_ms");
    CURL *curl = curl_easy_init();
    Buffer buffer = {0};
    struct curl_slist *headers = curl_slist_append(NULL, "Content-Type: application/json");
    CURLcode status = CURLE_FAILED_INIT;
    long http_status = 0;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) == 0) {
        int64_t elapsed_ms = (int64_t)(now.tv_sec - started.tv_sec) * 1000 +
                             (now.tv_nsec - started.tv_nsec) / 1000000;
        if (elapsed_ms >= budget_ms) {
            budget_ms = 0;
            status = CURLE_OPERATION_TIMEDOUT;
        } else {
            budget_ms -= (unsigned)elapsed_ms;
        }
    } else {
        budget_ms = 0;
    }
    if (curl && headers && budget_ms > 0) {
        json_object_object_add(body, "timeout_ms", json_object_new_int64(budget_ms));
        char target[sizeof(endpoint)];
        strcpy(target, endpoint);
        if (tasks_enabled && request->type != MESSAGE_CMD_REQUEST &&
            request->type != MESSAGE_AGENT_REQUEST) {
            strcpy(target + strlen(target) - 6, "/agent");
        }
        curl_easy_setopt(curl, CURLOPT_URL, target);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_object_to_json_string(body));
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, (long)budget_ms);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
        curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, transfer_progress);
        curl_easy_setopt(curl, CURLOPT_XFERINFODATA, lifetime);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive_body);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
        status = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_status);
    }
    curl_easy_cleanup(curl);
    curl_slist_free_all(headers);
    json_object_put(body);
    json_object *result = status == CURLE_OK ? json_parse_object(buffer.data, buffer.length) : NULL;
    json_object *text = NULL;
    json_object *sources = NULL;
    json_object *code = NULL;
    json_object *message = NULL;
    bool success = result && http_status == 200 &&
                   json_object_object_get_ex(result, "text", &text) &&
                   json_object_is_type(text, json_type_string) &&
                   json_object_object_get_ex(result, "sources", &sources) &&
                   json_object_is_type(sources, json_type_array);
    json_object *kind = result == NULL ? NULL : json_object_object_get(result, "kind");
    bool proposal = tasks_enabled && http_status == 200 &&
        json_object_is_type(kind, json_type_string) &&
        strcmp(json_object_get_string(kind), "command_proposal") == 0;
    bool failure = result && http_status >= 400 &&
                   json_object_object_get_ex(result, "code", &code) &&
                   json_object_is_type(code, json_type_string) &&
                   json_object_object_get_ex(result, "message", &message) &&
                   json_object_is_type(message, json_type_string);
    if (failure) {
        const char *value = json_object_get_string(code);
        failure = strcmp(value, "INVALID_REQUEST") == 0 || strcmp(value, "SERVER_BUSY") == 0 ||
                  strcmp(value, "UPSTREAM_TIMEOUT") == 0 ||
                  strcmp(value, "UPSTREAM_UNAVAILABLE") == 0 ||
                  strcmp(value, "INTERNAL_ERROR") == 0;
    }
    if (success || failure || proposal) {
        response->type = MESSAGE_ERROR;
        if (proposal) {
            response->type = MESSAGE_COMMAND_PROPOSAL;
        } else if (success) {
            response->type = MESSAGE_RESPONSE;
        }
        response->request_id = request->request_id;
        response->body = buffer.data;
        response->body_length = buffer.length;
        json_object_put(result);
        return 0;
    }
    if (result) {
        json_object_put(result);
    }
    fprintf(stderr, "[%s] upstream req=%llu curl=%d http=%ld bytes=%zu\n",
            worker_lifetime_cancelled(lifetime) ? "INFO" : "WARN",
            (unsigned long long)request->request_id, (int)status, http_status, buffer.length);
    free(buffer.data);
    return frame_make_error(request->request_id,
                            status == CURLE_OPERATION_TIMEDOUT ? "UPSTREAM_TIMEOUT"
                                                               : "UPSTREAM_UNAVAILABLE",
                            "Agent 服务不可用或响应非法",
                            response);
}

int agent_http_enable_sessions(const char *server_id)
{
    size_t size = strlen(endpoint);
    if (strlen(server_id) != 32 || size < 6 || strcmp(endpoint + size - 6, "/agent") != 0 ||
        (strncmp(endpoint, "http://127.0.0.1:", 17) != 0 &&
         strncmp(endpoint, "http://localhost:", 17) != 0)) {
        return -1;
    }
    strcpy(session_server_id, server_id);
    return 0;
}

bool agent_http_sync_sessions(const char *snapshot)
{
    char url[1100];
    size_t prefix = strlen(endpoint) - 6;
    snprintf(url, sizeof(url), "%.*s/internal/sessions/sync", (int)prefix, endpoint);
    CURL *curl = curl_easy_init();
    struct curl_slist *headers = curl_slist_append(NULL, "Content-Type: application/json");
    Buffer buffer = {0};
    CURLcode status = CURLE_FAILED_INIT;
    long http_status = 0;
    if (curl != NULL && headers != NULL) {
        curl_easy_setopt(curl, CURLOPT_URL, url);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, snapshot);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 5000L);
        curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 1000L);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive_body);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
        status = curl_easy_perform(curl);
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_status);
    }
    json_object *body = status == CURLE_OK ? json_parse_object(buffer.data, buffer.length) : NULL;
    bool ok = http_status == 200 && body != NULL &&
              json_object_get_boolean(json_object_object_get(body, "ok"));
    if (body != NULL) {
        json_object_put(body);
    }
    free(buffer.data);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return ok;
}
