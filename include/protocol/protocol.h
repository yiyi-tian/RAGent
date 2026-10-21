#ifndef RAGENT_PROTOCOL_H
#define RAGENT_PROTOCOL_H

#include <json-c/json.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RAGENT_HEADER_SIZE 20U
#define RAGENT_MAX_BODY_SIZE (1024U * 1024U)
#define RAGENT_MAGIC 0x52414754U

typedef enum {
    MESSAGE_CHAT_REQUEST = 1,
    MESSAGE_RAG_REQUEST = 2,
    MESSAGE_CMD_REQUEST = 3,
    MESSAGE_AGENT_REQUEST = 4,
    MESSAGE_RESPONSE = 100,
    MESSAGE_ERROR = 101,
    MESSAGE_PING = 200,
    MESSAGE_PONG = 201
} MessageType;

typedef struct {
    MessageType type;
    uint64_t request_id;
    unsigned char *body;
    size_t body_length;
} Frame;

typedef struct {
    unsigned char *data;
    size_t length;
    size_t capacity;
} FrameDecoder;

void frame_decoder_init(FrameDecoder *decoder);
void frame_decoder_destroy(FrameDecoder *decoder);
bool frame_decoder_empty(const FrameDecoder *decoder);
int frame_decoder_append(FrameDecoder *decoder, const void *data, size_t length);
/* 返回1表示得到完整帧，0表示数据不足，-1表示帧头非法。调用方释放frame.body。 */
int frame_decoder_next(FrameDecoder *decoder, Frame *frame, const char **error_message);
int frame_encode(const Frame *frame, unsigned char **encoded, size_t *encoded_length);
void frame_destroy(Frame *frame);
json_object *json_parse_object(const unsigned char *body, size_t body_length);
bool text_is_blank(const char *text, size_t length);
int frame_make_error(uint64_t request_id, const char *code, const char *message, Frame *frame);
int handle_fixed_request(const Frame *request, Frame *response);

#endif
