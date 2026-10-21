#include "protocol/protocol.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

static uint64_t read_integer(const unsigned char *bytes, size_t offset, unsigned width)
{
    uint64_t value = 0;
    for (unsigned index = 0; index < width; ++index) {
        value = (value << 8) | bytes[offset + index];
    }
    return value;
}

static void write_integer(unsigned char *bytes, size_t offset, uint64_t value, unsigned width)
{
    for (unsigned index = 0; index < width; ++index) {
        const unsigned shift = (width - index - 1U) * 8U;
        bytes[offset + index] = (unsigned char)((value >> shift) & 0xffU);
    }
}

void frame_decoder_init(FrameDecoder *decoder)
{
    memset(decoder, 0, sizeof(*decoder));
}

void frame_decoder_destroy(FrameDecoder *decoder)
{
    free(decoder->data);
    memset(decoder, 0, sizeof(*decoder));
}

bool frame_decoder_empty(const FrameDecoder *decoder)
{
    return decoder->length == 0;
}

int frame_decoder_append(FrameDecoder *decoder, const void *data, size_t length)
{
    const size_t maximum = RAGENT_MAX_BODY_SIZE + RAGENT_HEADER_SIZE + 16384U;
    if (length > 16384U || decoder->length > maximum - length) {
        return -1;
    }
    const size_t required = decoder->length + length;
    if (required > decoder->capacity) {
        size_t capacity = decoder->capacity == 0 ? 16384U : decoder->capacity;
        while (capacity < required) {
            capacity = capacity > maximum / 2U ? maximum : capacity * 2U;
        }
        unsigned char *resized = realloc(decoder->data, capacity);
        if (resized == NULL) {
            return -1;
        }
        decoder->data = resized;
        decoder->capacity = capacity;
    }
    memcpy(decoder->data + decoder->length, data, length);
    decoder->length = required;
    return 0;
}

int frame_decoder_next(FrameDecoder *decoder, Frame *frame, const char **error_message)
{
    if (decoder->length < RAGENT_HEADER_SIZE) {
        return 0;
    }
    if (read_integer(decoder->data, 0, 4) != RAGENT_MAGIC) {
        *error_message = "magic错误";
        return -1;
    }
    if (read_integer(decoder->data, 4, 2) != 1) {
        *error_message = "version不支持";
        return -1;
    }
    const uint64_t body_length = read_integer(decoder->data, 8, 4);
    if (body_length > RAGENT_MAX_BODY_SIZE) {
        *error_message = "消息体长度超限";
        return -1;
    }
    const size_t frame_length = RAGENT_HEADER_SIZE + (size_t)body_length;
    if (decoder->length < frame_length) {
        return 0;
    }

    frame->type = (MessageType)read_integer(decoder->data, 6, 2);
    frame->request_id = read_integer(decoder->data, 12, 8);
    frame->body_length = (size_t)body_length;
    frame->body = malloc(frame->body_length == 0 ? 1 : frame->body_length);
    if (frame->body == NULL) {
        *error_message = "无法分配消息体";
        return -1;
    }
    memcpy(frame->body, decoder->data + RAGENT_HEADER_SIZE, frame->body_length);
    decoder->length -= frame_length;
    if (decoder->length > 0) {
        memmove(decoder->data, decoder->data + frame_length, decoder->length);
    }
    return 1;
}

int frame_encode(const Frame *frame, unsigned char **encoded, size_t *encoded_length)
{
    if (frame->body_length > RAGENT_MAX_BODY_SIZE) {
        return -1;
    }
    const size_t length = RAGENT_HEADER_SIZE + frame->body_length;
    unsigned char *bytes = malloc(length);
    if (bytes == NULL) {
        return -1;
    }
    write_integer(bytes, 0, RAGENT_MAGIC, 4);
    write_integer(bytes, 4, 1, 2);
    write_integer(bytes, 6, (uint16_t)frame->type, 2);
    write_integer(bytes, 8, frame->body_length, 4);
    write_integer(bytes, 12, frame->request_id, 8);
    if (frame->body_length > 0) {
        memcpy(bytes + RAGENT_HEADER_SIZE, frame->body, frame->body_length);
    }
    *encoded = bytes;
    *encoded_length = length;
    return 0;
}

void frame_destroy(Frame *frame)
{
    free(frame->body);
    memset(frame, 0, sizeof(*frame));
}

json_object *json_parse_object(const unsigned char *body, size_t body_length)
{
    if (body_length >= 3 && memcmp(body, "\xef\xbb\xbf", 3) == 0) {
        return NULL;
    }
    if (body_length > INT32_MAX) {
        return NULL;
    }
    struct json_tokener *tokener = json_tokener_new();
    if (tokener == NULL) {
        return NULL;
    }
    json_tokener_set_flags(tokener, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    json_object *value = json_tokener_parse_ex(tokener, (const char *)body, (int)body_length);
    const enum json_tokener_error error = json_tokener_get_error(tokener);
    const size_t parsed_length = json_tokener_get_parse_end(tokener);
    json_tokener_free(tokener);
    if (error != json_tokener_success || value == NULL || !json_object_is_type(value, json_type_object)) {
        if (value != NULL) {
            json_object_put(value);
        }
        return NULL;
    }
    for (size_t index = parsed_length; index < body_length; ++index) {
        if (!isspace(body[index])) {
            json_object_put(value);
            return NULL;
        }
    }
    return value;
}

bool text_is_blank(const char *text, size_t length)
{
    for (size_t index = 0; index < length; ++index) {
        if (text[index] != ' ' && text[index] != '\t' && text[index] != '\r' &&
            text[index] != '\n' && text[index] != '\v' && text[index] != '\f') {
            return false;
        }
    }
    return true;
}

static int frame_set_body(Frame *frame, MessageType type, uint64_t request_id,
                          const char *body, size_t body_length)
{
    frame->body = malloc(body_length == 0 ? 1 : body_length);
    if (frame->body == NULL) {
        return -1;
    }
    if (body_length > 0) {
        memcpy(frame->body, body, body_length);
    }
    frame->type = type;
    frame->request_id = request_id;
    frame->body_length = body_length;
    return 0;
}

int frame_make_error(uint64_t request_id, const char *code, const char *message, Frame *frame)
{
    json_object *body = json_object_new_object();
    if (body == NULL) {
        return -1;
    }
    json_object_object_add(body, "code", json_object_new_string(code));
    json_object_object_add(body, "message", json_object_new_string(message));
    const char *encoded = json_object_to_json_string_ext(body, JSON_C_TO_STRING_PLAIN);
    const int result = frame_set_body(frame, MESSAGE_ERROR, request_id, encoded, strlen(encoded));
    json_object_put(body);
    return result;
}

int handle_fixed_request(const Frame *request, Frame *response)
{
    if (request->type != MESSAGE_CHAT_REQUEST && request->type != MESSAGE_RAG_REQUEST &&
        request->type != MESSAGE_CMD_REQUEST && request->type != MESSAGE_AGENT_REQUEST &&
        request->type != MESSAGE_PING) {
        return frame_make_error(request->request_id, "UNSUPPORTED_MESSAGE_TYPE",
                                "消息类型未知或方向不合法", response);
    }

    json_object *body = json_parse_object(request->body, request->body_length);
    if (body == NULL) {
        return frame_make_error(request->request_id, "INVALID_REQUEST",
                                "消息体必须是合法UTF-8 JSON对象", response);
    }
    if (request->type == MESSAGE_PING) {
        json_object_put(body);
        return frame_set_body(response, MESSAGE_PONG, request->request_id, "{}", 2);
    }

    json_object *query = NULL;
    if (!json_object_object_get_ex(body, "query", &query) ||
        !json_object_is_type(query, json_type_string)) {
        json_object_put(body);
        return frame_make_error(request->request_id, "INVALID_REQUEST",
                                "query必须是非空白字符串", response);
    }
    const char *query_text = json_object_get_string(query);
    const size_t query_length = (size_t)json_object_get_string_len(query);
    if (text_is_blank(query_text, query_length)) {
        json_object_put(body);
        return frame_make_error(request->request_id, "INVALID_REQUEST",
                                "query必须是非空白字符串", response);
    }

    json_object *result = json_object_new_object();
    json_object *text = json_object_new_string_len("server received: ", 17);
    if (result == NULL || text == NULL) {
        if (result != NULL) json_object_put(result);
        if (text != NULL) json_object_put(text);
        json_object_put(body);
        return -1;
    }
    const size_t prefix_length = (size_t)json_object_get_string_len(text);
    char *response_text = malloc(prefix_length + query_length + 1);
    if (response_text == NULL) {
        json_object_put(text);
        json_object_put(result);
        json_object_put(body);
        return -1;
    }
    memcpy(response_text, json_object_get_string(text), prefix_length);
    memcpy(response_text + prefix_length, query_text, query_length);
    response_text[prefix_length + query_length] = '\0';
    json_object_object_add(result, "text", json_object_new_string_len(response_text,
                                                                        (int)(prefix_length + query_length)));
    free(response_text);
    json_object_put(text);
    json_object_object_add(result, "sources", json_object_new_array());
    const char *encoded = json_object_to_json_string_ext(result, JSON_C_TO_STRING_PLAIN);
    const size_t encoded_length = strlen(encoded);
    int status;
    if (encoded_length > RAGENT_MAX_BODY_SIZE) {
        status = frame_make_error(request->request_id, "INTERNAL_ERROR",
                                  "编码后的响应超过消息体上限", response);
    } else {
        status = frame_set_body(response, MESSAGE_RESPONSE, request->request_id,
                                encoded, encoded_length);
    }
    json_object_put(result);
    json_object_put(body);
    return status;
}
