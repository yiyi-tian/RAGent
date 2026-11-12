#ifndef RAGENT_WORKSPACE_H
#define RAGENT_WORKSPACE_H
#include <json-c/json.h>

/* root_fd借用已打开的工作区目录；调用方持有会话锁。返回对象由调用方json_object_put。
 * 只处理受限UTF-8文本及目录，不启动程序，不接受工作区外路径。
 */
json_object *workspace_execute(int root_fd, json_object *request);
#endif
