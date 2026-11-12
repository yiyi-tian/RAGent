#define _GNU_SOURCE
#include "server/task_bridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>

static int64_t monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static unsigned active_tasks;

static void end_task(TaskBridge *bridge)
{
    if (bridge->state != NULL) {
        json_object_put(bridge->state);
        bridge->state = NULL;
        --active_tasks;
    }
    bridge->request_id = 0;
    bridge->waiting = false;
    bridge->continuation[0] = '\0';
}

void task_bridge_destroy(TaskBridge *bridge)
{
    end_task(bridge);
    if (bridge->workspace != NULL) {
        json_object_put(bridge->workspace);
        bridge->workspace = NULL;
    }
}

static int set_body(Frame *frame, json_object *value)
{
    const char *encoded = json_object_to_json_string_ext(value, JSON_C_TO_STRING_PLAIN);
    size_t length = strlen(encoded);
    if (length > RAGENT_MAX_BODY_SIZE) {
        return -1;
    }
    unsigned char *copy = malloc(length + 1);
    if (copy == NULL) {
        return -1;
    }
    memcpy(copy, encoded, length + 1);
    free(frame->body);
    frame->body = copy;
    frame->body_length = length;
    return 0;
}

static const char *text_field(json_object *value, const char *key)
{
    if (!json_object_is_type(value, json_type_object)) {
        return NULL;
    }
    json_object *field = json_object_object_get(value, key);
    if (!json_object_is_type(field, json_type_string)) {
        return NULL;
    }
    const char *text = json_object_get_string(field);
    return strlen(text) == (size_t)json_object_get_string_len(field) ? text : NULL;
}

static int reject(uint64_t id, const char *message, Frame *reply)
{
    return frame_make_error(id, "INVALID_REQUEST", message, reply) == 0 ? 1 : -1;
}

int task_bridge_input(TaskBridge *bridge, Frame *request, Frame *reply)
{
    bool initial = request->type == MESSAGE_CMD_REQUEST || request->type == MESSAGE_AGENT_REQUEST;
    bool continuing = request->type == MESSAGE_COMMAND_RESULT
        || request->type == MESSAGE_TASK_CANCEL || request->type == MESSAGE_TASK_HEARTBEAT;
    if (!initial && !continuing && request->type != MESSAGE_SESSION_OPEN) {
        return 0;
    }
    json_object *body = json_parse_object(request->body, request->body_length);
    if (body == NULL) {
        return reject(request->request_id, "任务消息必须为JSON对象", reply);
    }
    if (request->type == MESSAGE_SESSION_OPEN) {
        const char *name = text_field(body, "name");
        if (bridge->request_id != 0 || name == NULL || strlen(name) > 512) {
            json_object_put(body);
            return reject(request->request_id, "工作区登记非法或有活动任务", reply);
        }
        if (bridge->workspace != NULL) {
            json_object_put(bridge->workspace);
        }
        bridge->workspace = json_object_new_object();
        json_object_object_add(bridge->workspace, "name", json_object_new_string(name));
        json_object_object_add(bridge->workspace, "cwd", json_object_new_string("."));
        reply->type = MESSAGE_SESSION_READY;
        reply->request_id = request->request_id;
        int status = set_body(reply, bridge->workspace);
        json_object_put(body);
        return status == 0 ? 1 : -1;
    }
    json_object *next = json_object_new_object();
    int status;
    if (initial) {
        if (bridge->workspace == NULL || bridge->request_id != 0) {
            json_object_put(body);
            json_object_put(next);
            return reject(
                request->request_id, "请先登记本地工作区，且同连接只能有一个命令任务", reply);
        }
        Frame checked = { 0 };
        status = handle_fixed_request(request, &checked);
        if (status != 0 || checked.type == MESSAGE_ERROR) {
            json_object_put(body);
            json_object_put(next);
            if (status == 0) {
                *reply = checked;
                return 1;
            }
            return -1;
        }
        frame_destroy(&checked);
        bridge->request_id = request->request_id;
        bridge->mode = request->type;
        bridge->remaining_ms = 120000;
        json_object_object_add(
            next, "query", json_object_get(json_object_object_get(body, "query")));
        json_object_object_add(next, "workspace", json_object_get(bridge->workspace));
    } else {
        const char *token = text_field(body, "continuation");
        if (!bridge->waiting || request->request_id != bridge->request_id || token == NULL
            || strcmp(token, bridge->continuation) != 0 || bridge->state == NULL) {
            json_object_put(body);
            json_object_put(next);
            return reject(request->request_id, "续行已过期、已消费或不属于当前任务", reply);
        }
        if (request->type == MESSAGE_TASK_HEARTBEAT) {
            bridge->touched = time(NULL);
            reply->type = MESSAGE_TASK_STATUS;
            reply->request_id = request->request_id;
            json_object_object_add(next, "active", json_object_new_boolean(true));
            int status = set_body(reply, next);
            json_object_put(body);
            json_object_put(next);
            return status == 0 ? 1 : -1;
        }
        json_object_object_add(
            next, "query", json_object_get(json_object_object_get(bridge->state, "query")));
        json_object_object_add(next, "task_state", json_object_get(bridge->state));
        if (request->type == MESSAGE_TASK_CANCEL) {
            json_object_object_add(next, "cancelled", json_object_new_boolean(true));
            json_object *completed = json_object_object_get(body, "results");
            if (completed != NULL) {
                json_object_object_add(next, "results", json_object_get(completed));
            }
        } else {
            json_object *results = json_object_object_get(body, "results");
            if (!json_object_is_type(results, json_type_array)
                || json_object_array_length(results) > 4) {
                json_object_put(body);
                json_object_put(next);
                return reject(request->request_id, "命令结果列表非法", reply);
            }
            json_object_object_add(next, "results", json_object_get(results));
        }
        request->type = bridge->mode;
    }
    bridge->waiting = false;
    bridge->continuation[0] = '\0';
    bridge->touched = time(NULL);
    bridge->phase_ms = monotonic_ms();
    if (bridge->phase_ms < 0) {
        json_object_put(body);
        json_object_put(next);
        end_task(bridge);
        return -1;
    }
    json_object_object_add(next,
        "private_deadline_ms",
        json_object_new_int64(bridge->phase_ms + bridge->remaining_ms));
    status = set_body(request, next);
    json_object_put(body);
    json_object_put(next);
    if (status != 0) {
        end_task(bridge);
        return -1;
    }
    return 2;
}

int task_bridge_output(TaskBridge *bridge, Frame *response)
{
    if (bridge->request_id == 0 || response->request_id != bridge->request_id) {
        return 0;
    }
    int64_t elapsed = monotonic_ms() - bridge->phase_ms;
    if (elapsed < 0 || (uint64_t)elapsed >= bridge->remaining_ms) {
        uint64_t id = response->request_id;
        end_task(bridge);
        frame_destroy(response);
        return frame_make_error(id, "UPSTREAM_TIMEOUT", "任务累计计算时限已到", response);
    }
    bridge->remaining_ms -= (unsigned)elapsed;
    if (response->type != MESSAGE_COMMAND_PROPOSAL) {
        end_task(bridge);
        return 0;
    }
    json_object *body = json_parse_object(response->body, response->body_length);
    json_object *state = body == NULL ? NULL : json_object_object_get(body, "state");
    json_object *commands = body == NULL ? NULL : json_object_object_get(body, "commands");
    bool valid = json_object_is_type(state, json_type_object)
        && strlen(json_object_to_json_string_ext(state, JSON_C_TO_STRING_PLAIN)) <= 512 * 1024
        && json_object_is_type(commands, json_type_array) && json_object_array_length(commands) > 0
        && json_object_array_length(commands) <= 4;
    for (size_t index = 0; valid && index < json_object_array_length(commands); ++index) {
        json_object *entry = json_object_array_get_idx(commands, index);
        const char *id = text_field(entry, "id");
        const char *command = text_field(entry, "command");
        const char *cwd = text_field(entry, "cwd");
        valid = id != NULL && *id != '\0' && strlen(id) <= 128 && command != NULL
            && *command != '\0' && strlen(command) <= 16384 && cwd != NULL && strlen(cwd) <= 512;
    }
    unsigned char random[16];
    if (!valid || getrandom(random, sizeof(random), 0) != sizeof(random)) {
        if (body != NULL) {
            json_object_put(body);
        }
        end_task(bridge);
        uint64_t id = response->request_id;
        frame_destroy(response);
        return frame_make_error(id, "INTERNAL_ERROR", "任务提案非法，未执行", response);
    }
    for (size_t index = 0; index < sizeof(random); ++index) {
        snprintf(bridge->continuation + index * 2, 3, "%02x", random[index]);
    }
    if (bridge->state == NULL && active_tasks >= 64) {
        json_object_put(body);
        uint64_t id = response->request_id;
        end_task(bridge);
        frame_destroy(response);
        return frame_make_error(id, "SERVER_BUSY", "待续行任务已满，命令未执行", response);
    }
    if (bridge->state != NULL) {
        json_object_put(bridge->state);
    } else {
        ++active_tasks;
    }
    bridge->state = json_object_get(state);
    json_object_object_del(body, "state");
    json_object_object_add(body, "continuation", json_object_new_string(bridge->continuation));
    int status = set_body(response, body);
    json_object_put(body);
    if (status != 0) {
        end_task(bridge);
        return -1;
    }
    bridge->waiting = true;
    bridge->touched = time(NULL);
    return 0;
}

int task_bridge_expire(TaskBridge *bridge, Frame *response, time_t now)
{
    if (!bridge->waiting || now - bridge->touched < 900) {
        return 0;
    }
    uint64_t id = bridge->request_id;
    end_task(bridge);
    return frame_make_error(id, "UPSTREAM_TIMEOUT", "授权等待超时，任务已结束", response) == 0 ? 1
                                                                                               : -1;
}
