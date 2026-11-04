#ifndef RAGENT_AGENT_HTTP_H
#define RAGENT_AGENT_HTTP_H
#include "protocol/protocol.h"
/* 在线程启动前配置并复制 url；空字符串保留固定响应。成功返回 0，失败返回 -1。 */
int agent_http_configure(const char *url, unsigned timeout_ms);
/* 借用 request；成功时调用方持有 response.body，使用 frame_destroy 释放。
 * 返回 0 表示已生成业务响应或错误响应，-1 表示无法构造响应。
 */
int agent_http_request(const Frame *request, Frame *response);
#endif
