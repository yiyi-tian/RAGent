#define _GNU_SOURCE
#include "executor/observe.h"

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
    char path[] = "/tmp/ragent-observe-XXXXXX";
    assert(mkdtemp(path) != NULL);
    int root = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    assert(root >= 0);
    int regular = openat(root, "file", O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    int secret = openat(root, ".env", O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    assert(regular >= 0 && secret >= 0);
    assert(write(regular, "hello", 5) == 5);
    assert(write(secret, "private", 7) == 7);
    close(regular);
    close(secret);
    assert(symlinkat("/etc/passwd", root, "escape") == 0);
    assert(linkat(root, ".env", root, "alias", 0) == 0);
    pid_t child = fork();
    assert(child >= 0);
    if (child == 0) {
        assert(observe_restrict(root) == 0);
        int file = openat(root, "file", O_RDONLY | O_CLOEXEC);
        assert(file >= 0);
        close(file);
        assert(openat(root, "file", O_WRONLY | O_TRUNC | O_CLOEXEC) < 0);
        assert(openat(root, "created", O_CREAT | O_WRONLY | O_CLOEXEC, 0600) < 0);
        assert(unlinkat(root, "file", 0) < 0);
        assert(openat(root, ".env", O_RDONLY | O_CLOEXEC) < 0);
        assert(openat(root, "alias", O_RDONLY | O_CLOEXEC) < 0);
        assert(openat(root, "escape", O_RDONLY | O_CLOEXEC) < 0);
        assert(open("/etc/passwd", O_RDONLY | O_CLOEXEC) < 0);
        assert(socket(AF_INET, SOCK_STREAM, 0) < 0 && errno == EPERM);
        assert(socket(AF_INET6, SOCK_DGRAM, 0) < 0 && errno == EPERM);
        assert(socket(AF_UNIX, SOCK_STREAM, 0) < 0 && errno == EPERM);
        int source = 42;
        int target = 0;
        struct iovec input = {
            .iov_base = &source,
            .iov_len = sizeof(source),
        };
        struct iovec output = {
            .iov_base = &target,
            .iov_len = sizeof(target),
        };
        assert(process_vm_readv(getpid(), &output, 1, &input, 1, 0) < 0 && errno == EPERM);
        char *arguments[] = { "sh", "-c", "true", NULL };
        char *environment[] = { NULL };
        assert(execve("/bin/sh", arguments, environment) < 0);
        char *allowed_arguments[] = {"ls", NULL};
        assert(execve("/usr/bin/ls", allowed_arguments, environment) < 0 && errno == EPERM);
        _exit(23);
    }
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 23);
    assert(unlinkat(root, "file", 0) == 0);
    assert(unlinkat(root, ".env", 0) == 0);
    assert(unlinkat(root, "escape", 0) == 0);
    assert(unlinkat(root, "alias", 0) == 0);
    close(root);
    assert(rmdir(path) == 0);
    return 0;
}
