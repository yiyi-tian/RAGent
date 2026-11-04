#include "server/agent_http.h"
#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static char endpoint[1024];
static unsigned timeout = 12000;
typedef struct {
    unsigned char *data;
    size_t length;
} Buffer;

int agent_http_configure(const char *url, unsigned timeout_ms)
{
    if (strlen(url) >= sizeof(endpoint)) {
        return -1;
    }
    strcpy(endpoint, url);
    timeout = timeout_ms;
    return curl_global_init(CURL_GLOBAL_DEFAULT) == CURLE_OK ? 0 : -1;
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

int agent_http_request(const Frame *request, Frame *response)
{
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
    CURL *curl = curl_easy_init();
    Buffer buffer = {0};
    struct curl_slist *headers = curl_slist_append(NULL, "Content-Type: application/json");
    CURLcode status = CURLE_FAILED_INIT;
    long http_status = 0;
    if (curl && headers) {
        curl_easy_setopt(curl, CURLOPT_URL, endpoint);
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_object_to_json_string(body));
        curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, (long)timeout);
        curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
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
    if (success || failure) {
        response->type = success ? MESSAGE_RESPONSE : MESSAGE_ERROR;
        response->request_id = request->request_id;
        response->body = buffer.data;
        response->body_length = buffer.length;
        json_object_put(result);
        return 0;
    }
    if (result) {
        json_object_put(result);
    }
    free(buffer.data);
    return frame_make_error(request->request_id,
                            status == CURLE_OPERATION_TIMEDOUT ? "UPSTREAM_TIMEOUT"
                                                               : "UPSTREAM_UNAVAILABLE",
                            "Agent 服务不可用或响应非法",
                            response);
}
