#ifndef RAGENT_EXECUTOR_OBSERVE_H
#define RAGENT_EXECUTOR_OBSERVE_H

/* 在当前目录执行已验证的一级命令；边界不可用或非一级返回126，不调用Shell。 */
int observe_command(const char *command, int workspace, const char *cwd);

/* 对当前进程及后代施加只读边界；root借用，必须在独立子进程内调用。 */
int observe_restrict(int root);

#endif
