#ifndef RAGENT_AGENT_HTTP_H
#define RAGENT_AGENT_HTTP_H
#include "protocol/protocol.h"
typedef struct WorkerLifetime WorkerLifetime;
/* 在线程启动前配置并复制 url；空字符串保留固定响应。成功返回 0，失败返回 -1。 */
int agent_http_configure(const char *url, unsigned timeout_ms);
bool agent_http_tasks_enabled(void);
/* 借用 request；成功时调用方持有 response.body，使用 frame_destroy 释放。
 * remaining_ms 是扣除排队后的剩余预算；不会超过已配置 HTTP 时限。
 * 返回 0 表示已生成业务响应或错误响应，-1 表示无法构造响应。
 */
int agent_http_request(const Frame *request, unsigned remaining_ms, Frame *response);
/* lifetime为调用期间有效的借用引用；失效时终止上游HTTP传输。 */
int agent_http_request_controlled(const Frame *request, unsigned remaining_ms,
                                  const WorkerLifetime *lifetime, uint64_t connection_id,
                                  Frame *response);
/* 只在启动前设置实例标识；快照同步仅由维护工作线程调用。 */
int agent_http_enable_sessions(const char *server_id);
bool agent_http_sync_sessions(const char *snapshot);
#endif
