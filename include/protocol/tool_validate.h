#ifndef RAGENT_TOOL_VALIDATE_H
#define RAGENT_TOOL_VALIDATE_H

#include <json-c/json.h>

#define TOOL_EXECUTE_COMMAND "execute_command"

typedef enum
{
    TOOL_VALIDATE_ERR_UNKNOWN_TOOL = 1,
    TOOL_VALIDATE_ERR_MISSING_PARAM,
    TOOL_VALIDATE_ERR_INVALID_TYPE,
    TOOL_VALIDATE_ERR_EMPTY_CMD
} ToolValidateStatus;

typedef struct
{
    ToolValidateStatus status;
    char cmd[256];
    char args_json[1024];
    char error_msg[256];
} ToolCall;

int validate_tool_call(const char *tool_name, json_object *args, ToolCall *out);

#endif