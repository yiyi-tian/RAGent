#define _POSIX_C_SOURCE 200809L
#include "executor/command.h"

#include <assert.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t cancelled = 0;

static void cancel_command(int signal_number)
{
    (void)signal_number;
    cancelled = 1;
}

static int fixture(const char *mode)
{
    if (strcmp(mode, "--fail") == 0) {
        return 7;
    }
    if (strcmp(mode, "--environment") == 0) {
        puts(getenv("RAGENT_API_KEY") == NULL ? "missing" : "leaked");
        return 0;
    }
    if (strcmp(mode, "--exact") == 0 || strcmp(mode, "--excess") == 0) {
        size_t count = COMMAND_OUTPUT_LIMIT + (strcmp(mode, "--excess") == 0 ? 1U : 0U);
        for (size_t i = 0; i < count; ++i) {
            putchar('x');
        }
        return 0;
    }
    if (strcmp(mode, "--descendant") == 0) {
        pid_t child = fork();
        if (child < 0) {
            return 1;
        }
        if (child > 0) {
            printf("%ld\n", (long)child);
            return 0;
        }
    } else if (strcmp(mode, "--closed") == 0) {
        close(STDOUT_FILENO);
        close(STDERR_FILENO);
    } else if (strcmp(mode, "--flood") == 0) {
        while (true) {
            if (write(STDOUT_FILENO, "0123456789", 10) < 0) {
                return 1;
            }
        }
    }
    struct timespec pause = {
        .tv_sec = 8,
        .tv_nsec = 0,
    };
    nanosleep(&pause, NULL);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1) {
        return fixture(argv[1]);
    }
    assert(prctl(PR_SET_CHILD_SUBREAPER, 1) == 0);
    CommandResult result;
    const char *modes[] = {
        "--idle",
        "--closed",
        "--flood",
        "--fail",
        "--exact",
        "--excess",
        "--environment",
        "--descendant",
    };
    assert(setenv("RAGENT_API_KEY", "fixture-secret", 1) == 0);
    for (size_t i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i) {
        char *arguments[] = {
            argv[0],
            (char *)modes[i],
            NULL,
        };
        int status = command_run(argv[0], arguments, 250, &cancelled, &result);
        if (strcmp(modes[i], "--fail") == 0) {
            assert(status == -1 && result.exit_code == 7);
            assert(strcmp(result.code, "COMMAND_FAILED") == 0);
        } else if (strcmp(modes[i], "--exact") == 0) {
            assert(status == 0 && result.output_length == COMMAND_OUTPUT_LIMIT);
        } else if (strcmp(modes[i], "--environment") == 0) {
            assert(status == 0 && strcmp(result.output, "missing\n") == 0);
        } else {
            const char *expected = strcmp(modes[i], "--excess") == 0 ||
                                   strcmp(modes[i], "--flood") == 0
                                       ? "COMMAND_OUTPUT_LIMIT"
                                       : "COMMAND_TIMEOUT";
            assert(status == -1 && strcmp(result.code, expected) == 0);
        }
        if (strcmp(modes[i], "--descendant") == 0) {
            pid_t descendant = (pid_t)strtol(result.output, NULL, 10);
            assert(descendant > 0);
            int child_status;
            assert(waitpid(descendant, &child_status, 0) == descendant);
            assert(WIFSIGNALED(child_status) && WTERMSIG(child_status) == SIGKILL);
        }
    }
    assert(command_diagnose("cat /etc/passwd", 250, &cancelled, &result) == -1);
    assert(strcmp(result.code, "COMMAND_NOT_ALLOWED") == 0);
    struct sigaction action = {0};
    action.sa_handler = cancel_command;
    sigemptyset(&action.sa_mask);
    assert(sigaction(SIGALRM, &action, NULL) == 0);
    struct itimerval timer = {
        .it_interval = {0},
        .it_value = {
            .tv_sec = 0,
            .tv_usec = 50000,
        },
    };
    assert(setitimer(ITIMER_REAL, &timer, NULL) == 0);
    char *arguments[] = {
        argv[0],
        "--idle",
        NULL,
    };
    assert(command_run(argv[0], arguments, 3000, &cancelled, &result) == -1);
    assert(cancelled && result.signal_number == SIGKILL);
    puts("执行器超时、输出、退出状态、环境隔离、进程组与取消检查通过");
    return 0;
}
