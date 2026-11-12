#ifndef RAGENT_EXECUTOR_POLICY_H
#define RAGENT_EXECUTOR_POLICY_H

#include <json-c/json.h>
#include <stdbool.h>

#define POLICY_COMMAND_LIMIT 16384U

/* 输入为完整UTF-8命令；返回由调用方释放的策略结果，不执行命令或展开。 */
json_object *command_policy(const char *command);

/* 默认保护路径规则；调用者仍需用内核权限落实实际访问边界。 */
bool policy_protected_path(const char *path);
const char *const *policy_protected_names(void);
const char *const *policy_observer_names(void);

#endif
