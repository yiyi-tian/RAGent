#include "protocol/cmd_exec.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>
#include <errno.h>

// 执行系统命令，并捕获输出（带超时强杀和输出截断）
int execute_command_safely(const char *cmd, char *const args[], char *output, size_t max_output_len) {
    int pipefd[2];
    if (pipe(pipefd) == -1) {
        snprintf(output, max_output_len, "Error: 创建管道失败");
        return -1;
    }

    pid_t pid = fork();
    if (pid == -1) {
        snprintf(output, max_output_len, "Error: 创建子进程失败");
        return -1;
    }

    if (pid == 0) {
        // ================= 子进程 =================
        close(pipefd[0]); 
        dup2(pipefd[1], STDOUT_FILENO); // 标准输出到管道
        dup2(pipefd[1], STDERR_FILENO); // 错误输出也到管道
        close(pipefd[1]);

        execvp(cmd, args);
        
        // 如果 execvp 失败（比如命令不存在）
        perror("execvp");
        exit(1);
    } else {
        // ================= 父进程 =================
        close(pipefd[1]);
        
        ssize_t total_read = 0;
        ssize_t bytes_read;
        char buffer[512];
        
        // 为了防止子进程卡死，父进程最多等 3 秒（你可以调整）
        time_t start_time = time(NULL);
        
        // 循环读取管道，直到读不到数据或者缓冲区满
        while ((bytes_read = read(pipefd[0], buffer, sizeof(buffer) - 1)) > 0) {
            // 如果总长度快超过缓冲区，就强行截断，不再读了
            if (total_read + bytes_read >= (ssize_t)max_output_len - 1) {
                ssize_t remain = max_output_len - 1 - total_read;
                memcpy(output + total_read, buffer, remain);
                total_read += remain;
                break; // 缓冲区满了，退出读取
            }
            
            memcpy(output + total_read, buffer, bytes_read);
            total_read += bytes_read;
            
            // 超时检查
            if (time(NULL) - start_time > 3) {
                // 超时了，给子进程发送 SIGKILL 强杀信号
                kill(pid, SIGKILL);
                // 补上一句提示
                const char *timeout_msg = "\n[Error: Command execution timed out]";
                ssize_t remain = max_output_len - 1 - total_read;
                if (remain > (ssize_t)strlen(timeout_msg)) {
                    memcpy(output + total_read, timeout_msg, strlen(timeout_msg));
                    total_read += strlen(timeout_msg);
                }
                break;
            }
        }
        
        output[total_read] = '\0'; // 加上字符串结束符
        close(pipefd[0]);

        int status;
        waitpid(pid, &status, 0); // 等待子进程彻底结束
        
        return 0;
    }
}
