#define _POSIX_C_SOURCE 200809L
#include "executor/command.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int64_t monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1;
    }
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void initialize_result(CommandResult *result)
{
    memset(result, 0, sizeof(*result));
    result->code = "COMMAND_FAILED";
    result->exit_code = -1;
}

static int collect_output(int fd, CommandResult *result, bool *eof, size_t limit)
{
    char buffer[4096];
    ssize_t count = read(fd, buffer, sizeof(buffer));
    if (count == 0) {
        *eof = true;
        return 0;
    }
    if (count < 0) {
        return errno == EINTR || errno == EAGAIN ? 0 : -1;
    }
    size_t available = limit - result->output_length;
    size_t copied = (size_t)count < available ? (size_t)count : available;
    memcpy(result->output + result->output_length, buffer, copied);
    result->output_length += copied;
    result->output[result->output_length] = '\0';
    if ((size_t)count > available) {
        result->code = "COMMAND_OUTPUT_LIMIT";
        return -1;
    }
    return 0;
}

int command_capture(const char *path, char *const argv[], const char *input, size_t input_length,
                    size_t output_limit, unsigned timeout_ms,
                    const volatile sig_atomic_t *cancelled, CommandResult *result)
{
    initialize_result(result);
    int64_t started = monotonic_ms();
    if (started < 0 || *cancelled || output_limit > COMMAND_CAPTURE_LIMIT) {
        return -1;
    }
    int descriptors[2];
    int incoming[2];
    if (pipe(descriptors) != 0) {
        return -1;
    }
    if (pipe(incoming) != 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return -1;
    }
    int read_flags = fcntl(descriptors[0], F_GETFL);
    int write_flags = fcntl(incoming[1], F_GETFL);
    if (read_flags < 0 || write_flags < 0 ||
        fcntl(descriptors[0], F_SETFL, read_flags | O_NONBLOCK) < 0 ||
        fcntl(incoming[1], F_SETFL, write_flags | O_NONBLOCK) < 0) {
        goto fail_pipes;
    }
    pid_t parent = getpid();
    pid_t child = fork();
    if (child < 0) {
        goto fail_pipes;
    }
    if (child == 0) {
        close(descriptors[0]);
        close(incoming[1]);
        if (prctl(PR_SET_PDEATHSIG, SIGKILL) != 0 || getppid() != parent ||
            setpgid(0, 0) != 0 || dup2(incoming[0], STDIN_FILENO) < 0 ||
            dup2(descriptors[1], STDOUT_FILENO) < 0 ||
            dup2(descriptors[1], STDERR_FILENO) < 0) {
            _exit(127);
        }
        close(incoming[0]);
        close(descriptors[1]);
        char *environment[] = {
            "PATH=/usr/bin:/bin",
            "LC_ALL=C",
            NULL,
        };
        execve(path, argv, environment);
        _exit(127);
    }
    close(descriptors[1]);
    close(incoming[0]);
    if (setpgid(child, child) != 0 && errno != EACCES && errno != ESRCH) {
        kill(child, SIGKILL);
    }
    size_t sent = 0;
    bool eof = false;
    bool exited = false;
    bool completed = false;
    int status = 0;
    while (true) {
        int64_t now = monotonic_ms();
        if (*cancelled || now < 0) {
            break;
        }
        if (now - started >= timeout_ms) {
            result->code = "COMMAND_TIMEOUT";
            break;
        }
        if (incoming[1] >= 0) {
            if (sent < input_length) {
                ssize_t count = write(incoming[1], input + sent, input_length - sent);
                if (count > 0) {
                    sent += (size_t)count;
                } else if (count < 0 && errno != EAGAIN && errno != EINTR) {
                    close(incoming[1]);
                    incoming[1] = -1;
                }
            }
            if (sent == input_length && incoming[1] >= 0) {
                close(incoming[1]);
                incoming[1] = -1;
            }
        }
        if (!eof && collect_output(descriptors[0], result, &eof, output_limit) != 0) {
            break;
        }
        if (!exited) {
            pid_t waited = waitpid(child, &status, WNOHANG);
            if (waited == child) {
                exited = true;
            } else if (waited < 0 && errno != EINTR) {
                break;
            }
        }
        if (exited && eof) {
            completed = true;
            break;
        }
        int64_t remaining = timeout_ms - (now - started);
        int delay = remaining < 20 ? (int)remaining : 20;
        struct pollfd watched[] = {
            {.fd = eof ? -1 : descriptors[0], .events = POLLIN | POLLHUP},
            {.fd = incoming[1], .events = POLLOUT},
        };
        if (poll(watched, 2, delay) < 0 && errno != EINTR) {
            break;
        }
    }
    close(descriptors[0]);
    if (incoming[1] >= 0) {
        close(incoming[1]);
    }
    if (!completed) {
        kill(-child, SIGKILL);
        if (!exited) {
            kill(child, SIGKILL);
        }
    }
    if (!exited) {
        pid_t waited;
        do {
            waited = waitpid(child, &status, 0);
        } while (waited < 0 && errno == EINTR);
        if (waited != child) {
            return -1;
        }
    }
    if (WIFEXITED(status)) {
        result->exit_code = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        result->signal_number = WTERMSIG(status);
    }
    if (completed && result->exit_code == 0) {
        result->code = NULL;
        return 0;
    }
    return -1;
fail_pipes:
    close(descriptors[0]);
    close(descriptors[1]);
    close(incoming[0]);
    close(incoming[1]);
    return -1;
}

int command_run(const char *path, char *const argv[], unsigned timeout_ms,
                const volatile sig_atomic_t *cancelled, CommandResult *result)
{
    return command_capture(path, argv, NULL, 0, COMMAND_OUTPUT_LIMIT,
                           timeout_ms, cancelled, result);
}

int command_diagnose(const char *target, unsigned timeout_ms,
                     const volatile sig_atomic_t *cancelled, CommandResult *result)
{
    char *memory[] = {"free", "-b", NULL};
    char *disk[] = {"df", "-P", "-k", "/", NULL};
    char *processes[] = {"ps", "-eo", "pid,ppid,pcpu,pmem,comm", "--sort=-pcpu", NULL};
    if (timeout_ms == 0 || timeout_ms > COMMAND_TIMEOUT_MAX_MS) {
        initialize_result(result);
        return -1;
    }
    char *load[] = {"uptime", NULL};
    if (strcmp(target, "load") == 0) {
        return command_run("/usr/bin/uptime", load, timeout_ms, cancelled, result);
    }
    if (strcmp(target, "memory") == 0) {
        return command_run("/usr/bin/free", memory, timeout_ms, cancelled, result);
    }
    if (strcmp(target, "disk") == 0) {
        return command_run("/usr/bin/df", disk, timeout_ms, cancelled, result);
    }
    if (strcmp(target, "processes") == 0) {
        return command_run("/usr/bin/ps", processes, timeout_ms, cancelled, result);
    }
    initialize_result(result);
    result->code = "COMMAND_NOT_ALLOWED";
    return -1;
}
