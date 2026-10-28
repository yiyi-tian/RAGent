#include "protocol/tool_validate.h"
#include <ctype.h>
#include <string.h>
#include <stdio.h>

// 校验工具调用的合法性
// 返回值：0 表示校验通过，1 表示校验失败
int validate_tool_call(const char *tool_name, json_object *args, ToolCall *out)
{
    // 每次调用前，先把输出结构体清零，防止有脏数据
    memset(out, 0, sizeof(ToolCall));

    // 1. 检查工具名是否在白名单中（目前只允许 execute_command）
    if (!tool_name || strcmp(tool_name, TOOL_EXECUTE_COMMAND) != 0)
    {
        out->status = TOOL_VALIDATE_ERR_UNKNOWN_TOOL;
        snprintf(out->error_msg, sizeof(out->error_msg), "unknown tool: %s", tool_name ? tool_name : "(null)");
        return 1;
    }

    // 2. 检查参数里有没有 "cmd" 字段
    json_object *cmd_obj = NULL;
    if (!json_object_object_get_ex(args, "cmd", &cmd_obj))
    {
        out->status = TOOL_VALIDATE_ERR_MISSING_PARAM;
        snprintf(out->error_msg, sizeof(out->error_msg), "missing 'cmd' parameter");
        return 1;
    }

    // 3. 检查 "cmd" 字段是不是字符串类型
    if (!json_object_is_type(cmd_obj, json_type_string))
    {
        out->status = TOOL_VALIDATE_ERR_INVALID_TYPE;
        snprintf(out->error_msg, sizeof(out->error_msg), "'cmd' must be a string");
        return 1;
    }

    // 提取 cmd 字符串
    const char *cmd = json_object_get_string(cmd_obj);
    if (!cmd || cmd[0] == '\0')
    {
        out->status = TOOL_VALIDATE_ERR_EMPTY_CMD;
        snprintf(out->error_msg, sizeof(out->error_msg), "empty 'cmd' parameter");
        return 1;
    }

    // ================= 新增：安全过滤 =================
    // 4. 检查是否包含 Shell 注入字符 ( ; | & $ > < ` )
    if (strpbrk(cmd, ";|&$><`") != NULL)
    {
        out->status = TOOL_VALIDATE_ERR_INVALID_TYPE; // 借用类型错误的枚举
        snprintf(out->error_msg, sizeof(out->error_msg), "Security error: illegal characters in cmd");
        return 1;
    }

    // 5. 命令白名单校验
    const char *allowed_cmds[] = {"ls", "cat", "grep", "ps", "free", "top"};
    int is_allowed = 0;
    for (int i = 0; i < (int)(sizeof(allowed_cmds) / sizeof(allowed_cmds[0])); i++)
    {
        if (strcmp(cmd, allowed_cmds[i]) == 0)
        {
            is_allowed = 1;
            break;
        }
    }
    if (!is_allowed)
    {
        out->status = TOOL_VALIDATE_ERR_UNKNOWN_TOOL;
        snprintf(out->error_msg, sizeof(out->error_msg), "Security error: command '%s' is not in whitelist", cmd);
        return 1;
    }
    // ==================================================

    // 6. 如果一切顺利，说明校验通过，保存命令
    out->status = (ToolValidateStatus)0;
    snprintf(out->cmd, sizeof(out->cmd), "%s", cmd);

    // 7.把 args 也提取出来
    json_object *args_obj = NULL;
    if (json_object_object_get_ex(args, "args", &args_obj))
    {
        snprintf(out->args_json, sizeof(out->args_json), "%s", json_object_to_json_string(args_obj));
    }
    return 0;
}
