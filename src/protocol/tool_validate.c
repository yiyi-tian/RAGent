#include "protocol/tool_validate.h"
#include <string.h>
#include <stdio.h>

int validate_tool_call(const char *tool_name, json_object *args, ToolCall *out)
{
    memset(out, 0, sizeof(ToolCall));

    if (!tool_name || strcmp(tool_name, TOOL_EXECUTE_COMMAND) != 0)
    {
        out->status = TOOL_VALIDATE_ERR_UNKNOWN_TOOL;
        snprintf(out->error_msg, sizeof(out->error_msg), "unknown tool: %s", tool_name ? tool_name : "(null)");
        return 1;
    }

    json_object *cmd_obj = NULL;
    if (!json_object_object_get_ex(args, "cmd", &cmd_obj))
    {
        out->status = TOOL_VALIDATE_ERR_MISSING_PARAM;
        snprintf(out->error_msg, sizeof(out->error_msg), "missing 'cmd' parameter");
        return 1;
    }
    if (!json_object_is_type(cmd_obj, json_type_string))
    {
        out->status = TOOL_VALIDATE_ERR_INVALID_TYPE;
        snprintf(out->error_msg, sizeof(out->error_msg), "'cmd' must be a string");
        return 1;
    }
    const char *cmd = json_object_get_string(cmd_obj);
    if (!cmd || cmd[0] == '\0')
    {
        out->status = TOOL_VALIDATE_ERR_EMPTY_CMD;
        snprintf(out->error_msg, sizeof(out->error_msg), "empty 'cmd' parameter");
        return 1;
    }

    out->status = (ToolValidateStatus)0;
    snprintf(out->cmd, sizeof(out->cmd), "%s", cmd);

    json_object *args_obj = NULL;
    if (json_object_object_get_ex(args, "args", &args_obj))
    {
        snprintf(out->args_json, sizeof(out->args_json), "%s", json_object_to_json_string(args_obj));
    }
    return 0;
}