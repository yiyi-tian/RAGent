#ifndef RAGENT_TOOL_VALIDATE_H
#define RAGENT_TOOL_VALIDATE_H

#include <json-c/json.h>

// 定义允许执行的工具名
#define TOOL_EXECUTE_COMMAND "execute_command"

// 校验状态枚举：用于详细记录校验失败的原因
typedef enum
{
    // 注意：这里没有定义 OK = 0，而是从 1 开始定义各种错误状态。
    // 配合函数返回 int 类型，返回 0 代表成功，返回 1 代表失败。
    TOOL_VALIDATE_ERR_UNKNOWN_TOOL = 1, // 未知的工具（不在白名单内）
    TOOL_VALIDATE_ERR_MISSING_PARAM,    // 缺少必要参数（比如缺了 cmd）
    TOOL_VALIDATE_ERR_INVALID_TYPE,     // 参数类型错误（比如 cmd 不是字符串）
    TOOL_VALIDATE_ERR_EMPTY_CMD         // 命令内容为空
} ToolValidateStatus;

// 用于存放校验结果的结构体
typedef struct
{
    ToolValidateStatus status; // 记录具体的错误状态（失败时有用）
    char cmd[256];             // 校验通过后，提取出的命令名（如 "ls"）
    char args_json[1024];      // 校验通过后，提取出的参数原始 JSON 字符串
    char error_msg[256];       // 失败时，存放给用户看的错误提示信息
} ToolCall;

/**
 * 校验工具调用参数是否合法
 * @param tool_name 工具名称（例如 "execute_command"）
 * @param args 传入的 JSON 参数对象
 * @param out 校验结果的输出结构体（由调用方提供内存）
 * @return 0 表示校验完全通过，1 表示校验失败（被拦截）
 */
int validate_tool_call(const char *tool_name, json_object *args, ToolCall *out);

#endif
